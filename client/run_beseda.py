"""Точка входа клиента (её же собирает PyInstaller)."""
import sys

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--check", "--update", "--apply"):
        import updater                            # служебные команды обновления (и в exe-сборке)
        sys.exit(updater.main(sys.argv))
    from beseda.__main__ import main
    main()
