"""python -m bot.tui - the terminal UI for a running AgenticBotPlatform."""
from bot.tui.app import main
from bot.sandbox_ns import guard

if __name__ == "__main__":
    # No console window may ever appear on the desktop for anything this starts
    # (bot/sandbox_ns/guard.py). Installed here too, not just in the server: the TUI is its own
    # process, and it is the one that launches editors and helper programs.
    guard.install()
    main()
