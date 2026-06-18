"""claude-call — uma ligacao de voz com o seu Claude Code.

Pipeline (Pipecat):
  mic -> [EchoGate] -> VAD (Silero+SmartTurn) -> whisper (STT local)
      -> ClaudeBrain (daemon `claude` retomando a SUA sessao) -> edge-tts -> alto-falante

Cerebro = `claude` CLI ja autenticado (sua assinatura). Nao precisa de API key.
Rodar:  ./call.sh   (ou: uv run python call.py)
"""
import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from loguru import logger  # noqa: E402

from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.frames.frames import TTSSpeakFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.audio.vad_processor import VADProcessor  # noqa: E402
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams  # noqa: E402

import config as C  # noqa: E402
from brain import ClaudeBrain  # noqa: E402
from echo_gate import EchoGate  # noqa: E402
from session import latest_session  # noqa: E402
from stt import make_stt  # noqa: E402
from tts import make_tts  # noqa: E402

_FILLERS = {
    "en": ["One sec.", "Hold on.", "Let me check.", "Give me a moment."],
    "pt": ["Peraí.", "Deixa eu ver.", "Um segundo.", "Já te falo."],
    "es": ["Un momento.", "Déjame ver.", "Espera.", "Ya te digo."],
}


async def main():
    # Sessao: continua a SUA conversa mais recente desse diretorio (o diferencial).
    session_id = C.SESSION_ID
    if not session_id and C.CONTINUE:
        session_id = latest_session(C.CWD)
    if session_id:
        logger.info(f"continuando sua sessao do Claude Code: {session_id}")
    else:
        logger.info("sessao nova (nenhuma conversa anterior encontrada nesse diretorio)")

    # Transport
    use_echo_gate = C.ECHO_GATE
    if C.AEC:
        from extras_mac_aec import MacAECTransport  # opcional, so macOS
        transport = MacAECTransport()
        use_echo_gate = False
        logger.info("transport: macOS AEC (Voice Processing I/O)")
    else:
        transport = LocalAudioTransport(LocalAudioTransportParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=C.SAMPLE_RATE_IN, audio_out_sample_rate=C.SAMPLE_RATE_OUT,
        ))

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())

    stt = await make_stt(model=C.WHISPER_MODEL, language=C.WHISPER_LANG,
                         port=C.WHISPER_PORT, use_server=C.USE_WHISPER_SERVER)

    brain = ClaudeBrain(
        voice_rules=C.VOICE_RULES, model=C.MODEL, permission_flag=C.PERMISSION,
        wake_words=C.WAKE, active_window_secs=C.ACTIVE_WINDOW,
        fillers=_FILLERS.get(C.LANG[:2], _FILLERS["en"]),
        session_id=session_id, cwd=C.CWD,
    )

    try:
        tts = make_tts(provider=C.TTS, voice=C.VOICE, rate=C.VOICE_RATE,
                       sample_rate=C.SAMPLE_RATE_OUT, api_key=C.TTS_API_KEY, model=C.TTS_MODEL)
        if C.TTS != "edge":
            logger.info(f"TTS: {C.TTS} (premium)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"TTS '{C.TTS}' unavailable ({e}); falling back to free edge-tts")
        tts = make_tts(provider="edge", voice=C.EDGE_VOICE, rate=C.VOICE_RATE,
                       sample_rate=C.SAMPLE_RATE_OUT)

    stages = [transport.input()]
    if use_echo_gate:
        stages.append(EchoGate())
    stages += [vad, stt, brain, tts, transport.output()]
    pipeline = Pipeline(stages)

    # Idle timeout: por padrao 30 min de silencio; "0"/"off" = nunca encerra sozinha.
    idle_kwargs = ({"cancel_on_idle_timeout": False}
                   if C.IDLE_TIMEOUT.strip().lower() in ("0", "off", "none", "")
                   else {"idle_timeout_secs": float(C.IDLE_TIMEOUT)})
    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=C.SAMPLE_RATE_IN, audio_out_sample_rate=C.SAMPLE_RATE_OUT,
    ), **idle_kwargs)
    await task.queue_frames([TTSSpeakFrame(C.GREETING)])

    # No Windows (Proactor loop) add_signal_handler nao existe — o handle_sigint do
    # pipecat quebraria no start. Desligamos ele e tratamos o Ctrl+C na mao: cancela a
    # pipeline graciosamente (senao o KeyboardInterrupt deixa o shutdown sujo).
    runner = PipelineRunner(handle_sigint=(os.name != "nt"))
    if os.name == "nt":
        import signal
        loop = asyncio.get_running_loop()
        def _on_sigint(*_):
            loop.call_soon_threadsafe(lambda: loop.create_task(task.cancel(reason="Ctrl+C")))
        signal.signal(signal.SIGINT, _on_sigint)
    logger.info(f"claude-call no ar — voz={C.VOICE}, lang={C.LANG}, modelo={C.MODEL or 'default'}")
    await runner.run(task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
