import contextvars

# ContextVar to store the current request's OpenRouter API key (passed from frontend in header)
openrouter_key_var = contextvars.ContextVar("openrouter_key", default=None)
