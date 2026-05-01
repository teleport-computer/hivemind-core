import argparse
import sys

import docker

from .docker_runner import CONTAINER_LABEL, CONTAINER_LABEL_VALUE


def _build_container_limits(memory_mb: int, cpu_shares: int) -> dict:
    memory_bytes = int(memory_mb) * 1024 * 1024
    return {
        "memory": memory_bytes,
        "memswap": memory_bytes,
        "cpushares": int(cpu_shares),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Docker image with hivemind sandbox limits."
    )
    parser.add_argument("--path", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--network", default="")
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--cpu-shares", type=int, required=True)
    parser.add_argument("--docker-host", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = None
    try:
        client = (
            docker.DockerClient(base_url=args.docker_host)
            if args.docker_host
            else docker.from_env()
        )
        build_kwargs: dict = {
            "path": args.path,
            "tag": args.tag,
            "rm": True,
            "forcerm": True,
            "pull": False,
            "labels": {CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
            "container_limits": _build_container_limits(
                args.memory_mb,
                args.cpu_shares,
            ),
        }
        if args.network.strip():
            build_kwargs["network_mode"] = args.network.strip()
        client.images.build(**build_kwargs)
        return 0
    except Exception as exc:
        print(f"Docker image build failed for {args.tag}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
