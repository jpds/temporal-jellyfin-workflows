{
  description = "Temporal Jellyfin Content Recommender";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        python3 = pkgs.python3.override {
          packageOverrides = self: super: {

            # Both `griffe` and `griffelib` install the same Python package to
            # site-packages. Alias them to the same derivation so buildEnv
            # doesn't see a conflict when mcp (transitively) pulls in `griffe`
            # while openai-agents explicitly lists `griffelib`.
            griffe = self.griffelib;

            # Bump openai to satisfy openai-agents 0.17.5's >=2.36.0 requirement.
            # pythonRelaxDeps handles the version-constraint mismatches introduced
            # by bumping past what nixpkgs currently ships (2.33.0).
            openai = super.openai.overridePythonAttrs (_: rec {
              version = "2.41.1";
              src = pkgs.fetchPypi {
                pname = "openai";
                inherit version;
                hash = "sha256-I9YXoEMkV62ESXO+6PVAvp2pCJT3xWhoUtLTZdoFj1c=";
              };
              pythonRelaxDeps = true;
            });

            # Bump openai-agents from the nixpkgs 0.6.9 to 0.17.5.
            # We override propagatedBuildInputs because 0.17.5 gained several
            # new runtime deps (griffe, mcp, websockets) absent in 0.6.9.
            openai-agents = super.openai-agents.overridePythonAttrs (_: rec {
              version = "0.17.5";
              src = pkgs.fetchPypi {
                pname = "openai_agents";
                inherit version;
                hash = "sha256-XdRpQ7mT4aaKeKzSVPxqAM8EVfw9zIAgeOomlksUJ4w=";
              };
              pythonRelaxDeps = true;
              propagatedBuildInputs = with self; [
                griffelib
                mcp
                openai
                pydantic
                jellyfin-apiclient-python
                types-requests
                typing-extensions
                websockets
              ];
            });

          };
        };

        workflowLib = python3.pkgs.buildPythonPackage rec {
          pname = "jellyfin-recommender-lib";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm644 ${./activities.py} $out/${python3.sitePackages}/activities.py
            install -Dm644 ${./workflows.py}  $out/${python3.sitePackages}/workflows.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            jellyfin-apiclient-python
            openai-agents
            opentelemetry-api
            opentelemetry-sdk
            temporalio
          ];

          pythonImportsCheck = [
            "activities"
            "workflows"
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin recommender workflow libraries";
            license = licenses.mit;
          };
        };

        workerApp = python3.pkgs.buildPythonApplication rec {
          pname = "jellyfin-recommender-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./recommender-worker.py} $out/bin/recommender-worker.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            temporalio
            workflowLib
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin recommender worker";
            license = licenses.mit;
          };
        };

      in
      {
        packages.workerApp = workerApp;
        packages.default = workerApp;

        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.ruff
            pkgs.temporal-cli
            (python3.withPackages (ps: [
              ps.jellyfin-apiclient-python
              ps.openai-agents
              ps.opentelemetry-api
              ps.opentelemetry-sdk
              ps.temporalio
            ]))
          ];
        };

        checks.jellyfin-recommender = pkgs.testers.runNixOSTest (
          import ./test.nix {
            inherit workerApp pkgs;
            model = pkgs.fetchurl {
              url = "https://huggingface.co/unsloth/gemma-4-E2B-it-qat-GGUF/resolve/45dde4a86b6c5dce72297198762d2e8e68c0cbd4/gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf";
              hash = "sha256-zUUmST3Mv9Z5G+6IIuN+MDQAdNHU2araUs4Jr+/Wozo=";
            };
          }
        );
      }
    );
}
