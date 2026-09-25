# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def _resolve_hermes_home(args) -> Path:
    """Pick the Hermes home dir from --hermes-home, $HERMES_HOME, then default."""
    if args.hermes_home:
        # Refuse "." / "" / other clearly-not-a-Hermes-home values silently
        # mkdir'ing the plugin into CWD. Users hitting this almost certainly
        # meant ~/.hermes; force them to confirm.
        candidate = Path(args.hermes_home).expanduser().resolve()
        if str(candidate) in (str(Path.cwd().resolve()), str(Path("/").resolve())):
            print(
                f"Refusing --hermes-home={args.hermes_home!r}: resolves to "
                f"{candidate}, which is unlikely to be your real Hermes home. "
                "Pass a directory like ~/.hermes."
            )
            sys.exit(2)
        return candidate
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser()
    return Path("~/.hermes").expanduser()


def _resolve_install_python(hermes_home: Path) -> str:
    """Return the interpreter pip should install mempalace into.

    Prefer a venv under the Hermes home tree (POSIX layout first, then
    Windows). Fall back to the interpreter currently running this CLI.
    """
    venv_base = hermes_home / "hermes-agent" / "venv"
    candidates = (
        venv_base / "bin" / "python3",  # POSIX
        venv_base / "bin" / "python",
        venv_base / "Scripts" / "python.exe",  # Windows
    )
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def _resolve_install_palace_path() -> Path:
    """Resolve the palace_path mempalace will write/read at runtime.

    Honors ``MEMPALACE_PALACE_PATH`` so backfill files into the same palace
    the runtime provider will read from. The provider's ``_load_config``
    follows the same precedence (env > $HERMES_HOME/mempalace.json >
    default), so this stays consistent across install + runtime.
    """
    env_value = os.environ.get("MEMPALACE_PALACE_PATH")
    if env_value:
        return Path(env_value).expanduser()
    return Path("~/.mempalace/palace").expanduser()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via tmp-file-and-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _update_hermes_config_yaml(config_yaml: Path, provider: str) -> tuple[str, str]:
    """Set ``memory.provider`` in ``config_yaml`` to ``provider``.

    Returns ``(status, message)`` where ``status`` is one of:

    * ``"updated"`` — file was changed (new content written).
    * ``"noop"``    — file already had the desired provider, no write.
    * ``"error"``   — could not read, parse, or write the file.

    The caller (``cmd_hermes_install``) uses the status to decide the
    install's exit code: ``"error"`` is the only one that fails CI.
    Collapsing "already set" and "could not parse" into the same
    ``False`` return value (as the previous shape did) made the install
    silently succeed for bad YAML files.

    Uses ``yaml.safe_load`` / ``yaml.safe_dump`` for round-trip
    correctness on arbitrary layouts (anchors, mixed indentation, scalar
    ``memory: ~``). The trade-off is that ``safe_dump`` rewrites in
    canonical form — inline comments and unusual key ordering are lost.
    """
    try:
        import yaml
    except ImportError:
        return "error", "PyYAML not installed — cannot edit config.yaml safely."

    try:
        existing_raw = config_yaml.read_text(encoding="utf-8") if config_yaml.exists() else ""
        data = yaml.safe_load(existing_raw) if existing_raw.strip() else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return "error", f"{config_yaml} is not a YAML mapping; refusing to edit."

        memory = data.get("memory")
        if not isinstance(memory, dict):
            memory = {}
            data["memory"] = memory

        if memory.get("provider") == provider:
            return "noop", f"{config_yaml} already has memory.provider: {provider}"

        memory["provider"] = provider
        new_text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        _atomic_write_text(config_yaml, new_text)
        return "updated", f"Updated {config_yaml}: memory.provider = {provider}"
    except yaml.YAMLError as exc:
        return "error", f"Could not parse {config_yaml}: {exc}"
    except Exception as exc:
        return "error", f"Could not update {config_yaml}: {exc}"


def cmd_hermes_install(args):
    """Install the MemPalace memory provider plugin into a Hermes agent.

    Targets ``$HERMES_HOME/plugins/mempalace/`` — the canonical user-installed
    plugin directory that Hermes' ``discover_memory_providers()`` scans
    (``plugins/memory/__init__.py`` in hermes-agent). The bundled
    ``plugins/memory/<name>/`` tree inside the hermes-agent source is closed
    to new providers per CONTRIBUTING.md.
    """
    import importlib.util
    import shutil
    import subprocess

    # 1. Resolve target Hermes home + warn loudly if it doesn't look like
    #    Hermes is installed there. Don't abort — the user may be setting
    #    up Hermes for the first time — but surface the situation so the
    #    install doesn't silently succeed on a wrong directory.
    hermes_home = _resolve_hermes_home(args)
    looks_like_hermes = hermes_home.exists() and any(
        (hermes_home / candidate).exists() for candidate in ("config.yaml", "hermes-agent")
    )
    if not looks_like_hermes:
        print(
            f"Note: {hermes_home} doesn't look like a Hermes install yet "
            "(no config.yaml or hermes-agent/ found). Proceeding under the "
            "assumption you'll set it up afterwards."
        )
    hermes_home.mkdir(parents=True, exist_ok=True)

    # 2. Pick the Python that Hermes runs under, cross-platform.
    install_python = _resolve_install_python(hermes_home)

    # 3. Install (or upgrade) mempalace into that interpreter. ``--upgrade``
    #    matters: without it pip leaves an older mempalace in place and the
    #    user gets a stale build silently.
    print(f"Installing mempalace via {install_python} ...")
    proc = subprocess.run(
        [install_python, "-m", "pip", "install", "--quiet", "--upgrade", "mempalace"],
        check=False,
    )
    pip_failed = proc.returncode != 0
    if pip_failed:
        print(
            f"Warning: pip install returned {proc.returncode} — mempalace may not be "
            f"importable from {install_python}. Plugin files will still be copied."
        )

    # 4. Resolve the integration source files. They ship as the
    #    ``mempalace.integrations.hermes`` subpackage so this works
    #    identically in source-tree, editable, and wheel installs.
    spec = importlib.util.find_spec("mempalace.integrations.hermes")
    if spec is None or not spec.submodule_search_locations:
        print(
            "Error: could not locate mempalace.integrations.hermes — is mempalace "
            "installed correctly?"
        )
        sys.exit(1)
    integration_dir = Path(next(iter(spec.submodule_search_locations)))

    # 5. Drop plugin files into the user-installed plugin directory.
    plugin_dir = hermes_home / "plugins" / "mempalace"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    files_missing = []
    for fname in ("__init__.py", "backfill.py"):
        src = integration_dir / fname
        dst = plugin_dir / fname
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Copied {fname} -> {dst}")
        else:
            print(f"  Warning: source file not found: {src}")
            files_missing.append(fname)

    # 6. Write a plugin.yaml manifest so the directory-scan path picks the
    #    plugin up with the right metadata. Atomic write so a crashed install
    #    doesn't leave a half-written manifest behind.
    plugin_yaml = plugin_dir / "plugin.yaml"
    _atomic_write_text(
        plugin_yaml,
        "name: mempalace\n"
        "version: 1.0.0\n"
        'description: "MemPalace memory provider — verbatim, local, semantic recall across sessions."\n'
        "pip_dependencies:\n"
        "  - mempalace>=3.0.0\n"
        "hooks:\n"
        "  - on_session_end\n"
        "  - on_session_switch\n"
        "  - on_pre_compress\n"
        "  - on_memory_write\n"
        "  - on_delegation\n",
    )
    print(f"  Wrote {plugin_yaml}")

    # 7. Update Hermes config.yaml via real YAML parser/serialiser.
    config_yaml = hermes_home / "config.yaml"
    config_status, config_message = _update_hermes_config_yaml(config_yaml, "mempalace")
    config_updated = config_status == "updated"
    config_error = config_status == "error"
    if config_message:
        print(f"  {config_message}")

    # 8. Palace sanity check — honor MEMPALACE_PALACE_PATH so backfill files
    #    where the runtime provider will read from.
    palace_path = _resolve_install_palace_path()
    if not palace_path.exists():
        print(f"\nPalace at {palace_path} not initialized. Run: mempalace init <your-project-dir>")

    # 9. Optional backfill of existing Hermes sessions.
    do_backfill = False
    sessions_mined = 0
    backfill_error = False
    if not args.skip_backfill:
        if args.yes:
            do_backfill = True
        else:
            answer = (
                input(
                    f"\nMine existing Hermes sessions from {hermes_home}/sessions into "
                    f"{palace_path}? [y/N] "
                )
                .strip()
                .lower()
            )
            do_backfill = answer in ("y", "yes")

        if do_backfill:
            try:
                spec = importlib.util.spec_from_file_location(
                    "_mempalace_backfill",
                    plugin_dir / "backfill.py",
                )
                if spec is None or spec.loader is None:
                    raise ImportError("backfill.py not loadable")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                sessions_dir = hermes_home / "sessions"
                sessions_mined = module.backfill(
                    sessions_dir=sessions_dir,
                    palace_path=str(palace_path),
                )
            except Exception as exc:
                print(f"Backfill error: {exc}")
                backfill_error = True

    # 10. Summary + non-zero exit when something material went wrong so CI /
    #     scripted installs can detect failure.
    print("\n✓ MemPalace provider installed in Hermes")
    print(f"  Plugin directory: {plugin_dir}")
    if config_updated:
        print("✓ config.yaml updated: memory.provider = mempalace")
    if do_backfill and not backfill_error:
        print(f"✓ {sessions_mined} sessions mined into palace")
    elif not do_backfill:
        print("  Backfill skipped")
    print()
    print("Next steps:")
    print("  1. mempalace init ~/your-project   (if you haven't already)")
    print("  2. Restart Hermes (hermes gateway start, or your usual entrypoint)")
    print("  3. Verify: hermes memory status   (should show mempalace active)")

    if pip_failed or files_missing or backfill_error or config_error:
        sys.exit(1)
