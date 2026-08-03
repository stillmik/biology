from openai import OpenAI

_xai_client: OpenAI | None = None


def set_xai_client(client: OpenAI) -> None:
    global _xai_client
    _xai_client = client


def get_xai_client() -> OpenAI:
    if _xai_client is None:
        raise RuntimeError("xAI client has not been initialized")
    return _xai_client


def close_xai_client() -> None:
    global _xai_client
    if _xai_client is not None:
        _xai_client.close()
    _xai_client = None
