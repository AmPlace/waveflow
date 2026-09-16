import argparse
import os
import uvicorn


def configure_desktop_environment(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    os.environ["WAVEFLOW_MODE"] = "desktop"
    os.environ["WAVEFLOW_ALLOWED_ORIGINS"] = "null"
    os.environ["WAVEFLOW_SESSION_COOKIE_SECURE"] = "0"
    os.environ["WAVEFLOW_DB_PATH"] = os.path.join(data_dir, "waveflow.db")
    os.environ["RTSP_HLS_ROOT"] = os.path.join(data_dir, "rtsp_hls")
    # Plugin installations/environments are user data, not release-bundle
    # files. This keeps them across app updates and prevents the frozen
    # backend directory from becoming a mutable Plugin store.
    os.environ["WAVEFLOW_PLUGIN_ROOT"] = os.path.join(data_dir, "plugins")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-dir", default="")
    args = parser.parse_args()

    if args.data_dir:
        configure_desktop_environment(args.data_dir)

    from main import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
