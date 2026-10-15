import multiprocessing

if __name__ == "__main__":
    # loguru's enqueue=True makes multiprocessing start its resource tracker by re-running this executable.
    # freeze_support() diverts that child; without it every child would start another backend, recursively.
    multiprocessing.freeze_support()
    from dbaguy_backend.__main__ import main

    main()
