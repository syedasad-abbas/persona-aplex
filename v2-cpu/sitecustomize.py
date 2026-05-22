"""Runtime compatibility patches for the CPU container."""

try:
    import torch
except Exception:
    torch = None

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
    import os
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
        async def _skip_system_prompts_async(self, mimi, is_alive=None):
            print("Skipping system prompt steps (MOSHI_SKIP_SYSTEM_PROMPTS=1).")
            return None

        def _skip_system_prompts(self, mimi):
            print("Skipping system prompt steps (MOSHI_SKIP_SYSTEM_PROMPTS=1).")
            return None

        _moshi_lm.LMGen.step_system_prompts_async = _skip_system_prompts_async
        _moshi_lm.LMGen.step_system_prompts = _skip_system_prompts
