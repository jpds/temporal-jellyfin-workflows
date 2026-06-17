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

        commonLib = python3.pkgs.buildPythonPackage rec {
          pname = "jellyfin-workflows-lib";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            mkdir -p $out/${python3.sitePackages}
            install -Dm644 ${./common.py} $out/${python3.sitePackages}/common.py
            cp -r ${./recommendations}        $out/${python3.sitePackages}/recommendations
            cp -r ${./missing_seasons}        $out/${python3.sitePackages}/missing_seasons
            cp -r ${./director_completeness}  $out/${python3.sitePackages}/director_completeness
            cp -r ${./world_explorer}         $out/${python3.sitePackages}/world_explorer
          '';

          propagatedBuildInputs = with python3.pkgs; [
            jellyfin-apiclient-python
            openai-agents
            opentelemetry-api
            opentelemetry-sdk
            temporalio
          ];

          pythonImportsCheck = [
            "common"
            "recommendations"
            "missing_seasons"
            "director_completeness"
            "world_explorer"
          ];

          meta = with pkgs.lib; {
            description = "Shared Temporal Jellyfin workflow libraries";
            license = licenses.mit;
          };
        };

        recommendationsWorkerApp = python3.pkgs.buildPythonApplication rec {
          pname = "jellyfin-recommender-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./recommender-worker.py} $out/bin/recommender-worker.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            temporalio
            commonLib
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin recommender worker";
            license = licenses.mit;
          };
        };

        missingSeasonsWorkerApp = python3.pkgs.buildPythonApplication rec {
          pname = "jellyfin-missing-seasons-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./missing-seasons-worker.py} $out/bin/missing-seasons-worker.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            temporalio
            commonLib
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin missing seasons worker";
            license = licenses.mit;
          };
        };


        directorCompletenessWorkerApp = python3.pkgs.buildPythonApplication rec {
          pname = "jellyfin-director-completeness-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./director-completeness-worker.py} $out/bin/director-completeness-worker.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            temporalio
            commonLib
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin director completeness worker";
            license = licenses.mit;
          };
        };

        worldExplorerWorkerApp = python3.pkgs.buildPythonApplication rec {
          pname = "jellyfin-world-explorer-worker";
          version = "1.0.0";
          pyproject = false;

          dontUnpack = true;

          installPhase = ''
            install -Dm755 ${./world-explorer-worker.py} $out/bin/world-explorer-worker.py
          '';

          propagatedBuildInputs = with python3.pkgs; [
            temporalio
            commonLib
          ];

          meta = with pkgs.lib; {
            description = "Temporal Jellyfin world explorer worker";
            license = licenses.mit;
          };
        };

      in
      {
        packages.recommendationsWorkerApp = recommendationsWorkerApp;
        packages.missingSeasonsWorkerApp = missingSeasonsWorkerApp;
        packages.directorCompletenessWorkerApp = directorCompletenessWorkerApp;
        packages.worldExplorerWorkerApp = worldExplorerWorkerApp;
        packages.default = recommendationsWorkerApp;

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
            inherit recommendationsWorkerApp missingSeasonsWorkerApp directorCompletenessWorkerApp worldExplorerWorkerApp pkgs;
            model = pkgs.fetchurl {
              url = "https://huggingface.co/unsloth/gemma-4-E2B-it-qat-GGUF/resolve/45dde4a86b6c5dce72297198762d2e8e68c0cbd4/gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf";
              hash = "sha256-zUUmST3Mv9Z5G+6IIuN+MDQAdNHU2araUs4Jr+/Wozo=";
            };
          }
        );
      }
    );
}
