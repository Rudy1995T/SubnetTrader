"""Allow running as `python -m app.main` or `python -m app`."""
from app.main import entrypoint

if __name__ == "__main__":
    entrypoint()
