import multiprocessing

if __name__ == "__main__":
    # Defensive: if anything ever makes multiprocessing start a helper by re-running this frozen executable (loguru's
    # enqueue=True did, for its resource tracker), freeze_support() diverts that child instead of starting another
    # backend, recursively.
    multiprocessing.freeze_support()
    from dbaguy_backend.__main__ import main

    main()
