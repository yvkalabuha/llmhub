"""Entrypoint: ``python -m llmhub_infer``."""

import asyncio
import sys

from .supervisor import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
