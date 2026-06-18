"""Runtime compatibility patches for the CPU container."""

import os

try:
    import torch
except Exception:
    torch = None

def _env_int(name):
    try:
        value = os.getenv(name)
    except Exception:
        return None
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


if torch is not None:
    _cpu_threads = _env_int("MOSHI_CPU_THREADS") or _env_int("TORCH_NUM_THREADS")
    _interop_threads = _env_int("MOSHI_CPU_INTEROP_THREADS")
    if _cpu_threads:
        torch.set_num_threads(_cpu_threads)
    if _interop_threads:
        try:
            torch.set_num_interop_threads(_interop_threads)
        except RuntimeError:
            pass

if torch is not None and not torch.cuda.is_available():
    _torch_load = torch.load

    def _cpu_torch_load(*args, **kwargs):
        kwargs.setdefault("map_location", torch.device("cpu"))
        return _torch_load(*args, **kwargs)

    torch.load = _cpu_torch_load


# PersonaPlex CPU warmup can spend many minutes replaying voice-prompt
# embeddings before it sends the websocket handshake. For telephony we need a
# live session quickly; these env-gated patches keep the behavior reversible.
try:
    from moshi.models import lm as _moshi_lm
except Exception:
    _moshi_lm = None

if _moshi_lm is not None:
    if os.getenv("MOSHI_AUDIO_SILENCE_FRAME_CNT") is not None:
        _lmgen_init = _moshi_lm.LMGen.__init__

        def _patched_lmgen_init(self, *args, **kwargs):
            kwargs["audio_silence_frame_cnt"] = int(os.getenv("MOSHI_AUDIO_SILENCE_FRAME_CNT", "0"))
            return _lmgen_init(self, *args, **kwargs)

        _moshi_lm.LMGen.__init__ = _patched_lmgen_init

    if os.getenv("MOSHI_SKIP_VOICE_PROMPT_STEPS", "0").lower() in ("1", "true", "yes"):
        def _skip_voice_prompt_core(self, mimi):
            print("Skipping voice prompt steps (MOSHI_SKIP_VOICE_PROMPT_STEPS=1).")
            if False:
                yield None

        _moshi_lm.LMGen._step_voice_prompt_core = _skip_voice_prompt_core


    if os.getenv("MOSHI_SKIP_SYSTEM_PROMPTS", "0").lower() in ("1", "true", "yes"):
        _keep_silence = os.getenv("MOSHI_SKIP_SYSTEM_PROMPTS_KEEP_SILENCE", "1").lower() in ("1", "true", "yes")

        async def _skip_system_prompts_async(self, mimi, is_alive=None):
            print("Skipping voice/text prompt steps (MOSHI_SKIP_SYSTEM_PROMPTS=1).")
            if _keep_silence:
                await self._step_audio_silence_async(is_alive)
            return None

        def _skip_system_prompts(self, mimi):
            print("Skipping voice/text prompt steps (MOSHI_SKIP_SYSTEM_PROMPTS=1).")
            if _keep_silence:
                self._step_audio_silence()
            return None

        _moshi_lm.LMGen.step_system_prompts_async = _skip_system_prompts_async
        _moshi_lm.LMGen.step_system_prompts = _skip_system_prompts
