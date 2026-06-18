"""ClaudeBrain — o cerebro da call é o PROPRIO Claude Code, como daemon persistente.

Em vez de spawnar um `claude -p` novo a cada fala (cold, recarrega o contexto toda vez),
sobe UM processo `claude` em modo stream-json que fica vivo a call inteira:

  - o mic "conecta" nele escrevendo cada fala no stdin (mensagens JSON)
  - ele streama a resposta no stdout (falamos frase a frase)
  - contexto + cache ficam QUENTES entre os turnos (rapido depois do 1o)
  - com --resume <sua sessao>, o daemon É a sua conversa do Claude Code

Honestidade: nem um daemon "pensa" entre as falas — cada fala dispara uma inferencia
(o modelo roda nos servidores da Anthropic). O daemon mantem a sessao quente e viva;
nao e uma consciencia continua. Mas e um processo unico que o mic alimenta.
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import time
import uuid

from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

_SENT_BOUNDARY = re.compile(r"[.!?…](?=\s)|\n")

# Emojis e simbolos pictograficos — TTS nunca deve ler isso (😀 ✅ 🎉 → ⚠).
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # emoticons, pictographs, transport, symbols ext
    "\U00002600-\U000027BF"   # misc symbols + dingbats (✅ ⚠ ✨ ✓ ...)
    "\U0001F1E6-\U0001F1FF"   # bandeiras
    "\U00002190-\U000021FF"   # setas
    "\U00002300-\U000023FF"   # misc tecnico (⏰ ⌨ ...)
    "\U00002B00-\U00002BFF"   # setas/simbolos diversos (⭐ ...)
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _clean_for_speech(s: str) -> str:
    """Tira markdown E emojis antes de falar — TTS nao deve ler *, #, `, links, 😀, ✅."""
    s = s.replace("```", " ")
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"[*_~#>|]+", "", s)
    s = re.sub(r"^\s*[-•·]\s*", "", s)
    s = _EMOJI.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Fragmentos so de pontuacao ("--", "...", "|") fazem o edge-tts retornar "no audio"
    # e engasgam a fala — pula se nao sobrou nada falavel (sem letra/numero).
    return s if re.search(r"[^\W_]", s) else ""


class ClaudeBrain(FrameProcessor):
    def __init__(
        self,
        *,
        voice_rules: str,
        model: str | None = None,
        permission_flag: str = "--dangerously-skip-permissions",
        wake_words: list[str] | None = None,
        active_window_secs: float = 25.0,
        fillers: list[str] | None = None,
        session_id: str | None = None,
        cwd: str | None = None,
    ):
        super().__init__()
        self._voice_rules = voice_rules
        self._cwd = cwd or os.getcwd()
        self._model = model
        self._permission_flag = permission_flag
        self._wake = [w.lower() for w in (wake_words or []) if w.strip()]
        self._active_window = active_window_secs
        self._fillers = fillers or ["One sec.", "Hold on.", "Let me check.", "Give me a moment."]
        self._filler_i = 0

        self._resume = session_id          # se setado, RETOMA essa conversa
        self._session_id = session_id or str(uuid.uuid4())

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._buf = ""
        self._discard = False
        self._narrated_tool = False
        self._spoke = False
        self._active_until = 0.0

    def _should_answer(self, text: str) -> bool:
        if not self._wake:
            return True
        low = text.lower()
        return any(w in low for w in self._wake) or time.monotonic() < self._active_until

    def _build_cmd(self) -> list[str]:
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
        ]
        cmd += ["--resume", self._session_id] if self._resume else ["--session-id", self._session_id]
        if self._permission_flag:
            cmd += [self._permission_flag]
        if self._model:
            cmd += ["--model", self._model]
        cmd += ["--append-system-prompt", self._voice_rules]
        return cmd

    async def _ensure_proc(self):
        if self._proc and self._proc.returncode is None:
            return
        cmd = self._build_cmd()
        logger.info(f"[brain] up ({'resume '+self._session_id if self._resume else 'new session'})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=2**20, env={**os.environ},
        )
        self._reader = self.create_task(self._read_loop())
        self.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        try:
            async for raw in self._proc.stderr:
                line = raw.decode(errors="ignore").rstrip()
                if line:
                    logger.debug(f"[claude] {line}")
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, text: str):
        await self._ensure_proc()
        self._buf = ""
        self._discard = False
        self._narrated_tool = False
        self._spoke = False
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        payload = (json.dumps(msg) + "\n").encode()
        try:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.error("[brain] daemon dropped; restarting")
            self._proc = None
            await self._ensure_proc()
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()

    async def _read_loop(self):
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="ignore").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")

                if etype == "system" and ev.get("subtype") == "init":
                    sid = ev.get("session_id")
                    if sid:
                        self._session_id = sid
                        self._resume = sid
                    continue

                if etype == "stream_event":
                    inner = ev.get("event", {})
                    itype = inner.get("type")
                    if itype == "content_block_start":
                        if inner.get("content_block", {}).get("type") == "tool_use" \
                           and not self._narrated_tool and not self._discard:
                            self._narrated_tool = True
                            await self._say(self._next_filler())
                    elif itype == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta" and not self._discard:
                            self._buf += delta.get("text", "")
                            self._buf, sents = self._drain(self._buf)
                            for s in sents:
                                if await self._say(s):
                                    self._spoke = True

                elif etype == "result":
                    if not self._discard and await self._say(self._buf):
                        self._spoke = True
                    self._buf = ""
                    self._active_until = time.monotonic() + self._active_window
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[brain] read loop died")

    def _next_filler(self) -> str:
        f = self._fillers[self._filler_i % len(self._fillers)]
        self._filler_i += 1
        return f

    async def _say(self, text: str) -> bool:
        clean = _clean_for_speech(text)
        if not clean:
            return False
        await self.push_frame(TTSSpeakFrame(clean), FrameDirection.DOWNSTREAM)
        return True

    def _drain(self, buf: str):
        sents = []
        while True:
            m = _SENT_BOUNDARY.search(buf)
            if not m:
                break
            end = m.end()
            s = buf[:end].strip()
            buf = buf[end:]
            if s:
                sents.append(s)
        return buf, sents

    def _kill(self):
        if not (self._proc and self._proc.returncode is None):
            return
        try:
            if os.name == "nt":
                # Windows nao tem process groups/SIGKILL — derruba a arvore via taskkill
                # (o `claude` CLI roda em Node e pode ter filhos; /T pega todos).
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._discard = True
            self._buf = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame) and (frame.text or "").strip():
            text = frame.text.strip()
            if not self._should_answer(text):
                logger.debug(f"[brain] gated: {text!r}")
                return
            await self._send(text)
            return

        await self.push_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        if self._reader:
            self._reader.cancel()
        self._kill()
