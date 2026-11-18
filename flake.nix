{
  description = "MemPalace — mine projects and conversations into a verbatim, searchable memory palace (local-first, no API key)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        nixpkgs.follows = "nixpkgs";
      };
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
  };

  outputs = {
    self,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
    ...
  }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      # Parse uv.lock + pyproject.toml into a uv2nix workspace: the Nix build
      # then matches `uv` dependency-for-dependency.
      workspace = uv2nix.lib.workspace.loadWorkspace {
        workspaceRoot = ./.;
        # Editable installs (nix develop) import `editables` from hatchling's
        # editable-builder path; mempalace's [build-system].requires declares
        # only hatchling, so declare editables as a build dependency here.
        config = loaded: loaded // {
          extra-build-dependencies = loaded.extra-build-dependencies // {
            mempalace = [
              { requirement = pyproject-nix.lib.pep508.parseString "editables"; }
            ];
          };
        };
      };

      # Build local + third-party deps from wheels (uv's default source).
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      # Editable overlay: points `mempalace` at the checkout for development.
      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3;
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        )
      );
    in
    {
      # Development shell: a venv built from uv.lock via uv2nix, mirroring the
      # repo's documented workflow `uv sync --extra dev` (base deps + the `dev`
      # dependency-group: pytest, ruff, mypy, …). UV_NO_SYNC + the prebuilt venv
      # make uv skip re-resolving; UV_PYTHON points uv at the interpreter
      # uv2nix built. The spec names the `dev` group for the `mempalace`
      # project; base deps are always resolved, the group name activates the
      # dev tools.
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "mempalace-dev-env" (
            { mempalace =
                workspace.deps.default.mempalace ++ workspace.deps.groups.mempalace;
            }
          );
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      # Application outputs.
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
          inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
          venv = pythonSet.mkVirtualEnv "mempalace-env" workspace.deps.default;
        in
        {
          # Full venv: mempalace + runtime deps.
          default = venv;
          # Stripped app: links only the venv content present in
          # pythonSet.mempalace (binaries, man pages), without the
          # interpreter / activation-script cruft.
          mempalace = mkApplication {
            inherit venv;
            package = pythonSet.mempalace;
          };
        }
      );
    };
}
