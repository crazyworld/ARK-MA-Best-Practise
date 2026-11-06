"""Local dependency/configuration check. No API requests; never prints credentials."""
import json
import os
import sys


def main():
    result = {"python": sys.version.split()[0], "python_supported": sys.version_info >= (3, 10),
              "chat_endpoint_configured": bool(os.environ.get("LLM_RELAY_BASE_URL")),
              "chat_credential_configured": bool(os.environ.get("LLM_RELAY_API_KEY")),
              "embedding_endpoint_configured": bool(os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("LLM_RELAY_BASE_URL")),
              "embedding_credential_configured": bool(os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_RELAY_API_KEY")),
              "live_service_tested": False}
    try:
        import numpy
        result["numpy"] = numpy.__version__
    except ImportError:
        result["numpy"] = None
    print(json.dumps(result))
    return 0 if result["python_supported"] and result["numpy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
