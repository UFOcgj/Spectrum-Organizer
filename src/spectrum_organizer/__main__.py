from .safety.startup_cleanup import startup
from .single_instance import WindowsMutexBackend
from .product_runner import ProductRunnerDependencies, build_default_product_dependencies, check_task15_readiness


def _argv(argv):
    if argv is not None:
        return list(argv)
    return __import__("sys").argv[1:]


def main(instance_backend=None, local_appdata=None, window_launcher=None, argv=None) -> int:
    args = _argv(argv)
    if args[:1] == ["--origin-extraction-worker"]:
        from .origin.extraction_process import extraction_process_main

        return extraction_process_main(args[1:])
    if args[:1] == ["--origin-output-worker"]:
        from .origin.output_process import output_process_main

        return output_process_main(["output"])
    if args[:1] == ["--origin-verifier-worker"]:
        from .origin.output_process import output_process_main

        return output_process_main(["verifier"])
    if args[:1] == ["--pre-extraction-worker"]:
        from .pre_extraction_process import pre_extraction_process_main

        return pre_extraction_process_main(args[1:])
    backend = instance_backend or WindowsMutexBackend()
    startup_result = startup(backend, local_appdata=local_appdata)
    if startup_result.instance.should_exit:
        return 0
    launcher = window_launcher or _launch_main_window
    return int(launcher(startup_result))


def _launch_main_window(startup_result) -> int:
    from spectrum_organizer.ui.app import run_main_window

    return run_main_window(startup_result=startup_result, protected_paths=())


def startup_check_main(instance_backend=None, local_appdata=None) -> int:
    backend = instance_backend or WindowsMutexBackend()
    startup_result = startup(backend, local_appdata=local_appdata)
    if startup_result.instance.should_exit:
        return 0
    return 0


def readiness_main(deps: ProductRunnerDependencies | None = None, output=None) -> int:
    stream = output if output is not None else __import__("sys").stdout
    report = check_task15_readiness(deps or build_default_product_dependencies())
    status = "ready" if report.ready else "not ready"
    stream.write(f"{status}: {report.next_action}\n")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
