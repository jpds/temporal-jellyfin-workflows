{
  recommendationsWorkerApp,
  missingSeasonsWorkerApp,
  pkgs,
  # Path to a GGUF model file used by llama-server. The flake passes a
  # fetchurl derivation; override it to point to any GGUF on disk when
  # iterating locally.
  model,
  ...
}:

let
  authHeader = ''MediaBrowser Client="NixOS Integration Tests", DeviceId="jellyfin-rec-test", Device="TestDevice", Version="1.0.0"'';

  jellyfinSetupPayload = pkgs.writeText "auth.json" (builtins.toJSON { Username = "jellyfin"; });
  emptyPayload = pkgs.writeText "empty.json" (builtins.toJSON { });

  mkShow = id: name: [ { show = { inherit id name; }; } ];
  mkSeasons = map ({ n, d }: { number = n; premiereDate = d; });

  tvmazeMockRoot =
    let
      files = {
        "search-the-wire.json"     = mkShow 1 "The Wire";
        "search-breaking-bad.json" = mkShow 2 "Breaking Bad";
        "search-severance.json"    = mkShow 3 "Severance";
        "search-dark.json"         = mkShow 4 "Dark";
        "seasons-1.json" = mkSeasons [
          { n = 1; d = "2002-06-02"; } { n = 2; d = "2003-06-01"; }
          { n = 3; d = "2004-09-19"; } { n = 4; d = "2006-09-10"; }
          { n = 5; d = "2008-01-06"; }
        ];
        "seasons-2.json" = mkSeasons [
          { n = 1; d = "2008-01-20"; } { n = 2; d = "2009-03-08"; }
          { n = 3; d = "2010-03-21"; } { n = 4; d = "2011-07-17"; }
          { n = 5; d = "2012-07-15"; }
        ];
        "seasons-3.json" = mkSeasons [
          { n = 1; d = "2022-02-18"; } { n = 2; d = "2025-01-17"; }
          { n = 3; d = "2027-03-01"; }
        ];
        "seasons-4.json" = mkSeasons [
          { n = 1; d = "2017-12-01"; } { n = 2; d = "2019-06-21"; }
          { n = 3; d = "2020-06-27"; }
        ];
      };
    in
    pkgs.linkFarm "tvmaze-mock-root" (
      pkgs.lib.mapAttrsToList (name: data: {
        inherit name;
        path = pkgs.writeText name (builtins.toJSON data);
      }) files
    );

  tvmazeMockConfig = pkgs.writeText "Caddyfile" ''
    :8081 {
      root * ${tvmazeMockRoot}

      @searchWire {
        path /search/shows
        expression `{http.request.uri.query} == "q=The+Wire"`
      }
      @searchBB {
        path /search/shows
        expression `{http.request.uri.query} == "q=Breaking+Bad"`
      }
      @searchSev {
        path /search/shows
        query q=Severance
      }
      @searchDark {
        path /search/shows
        query q=Dark
      }

      handle @searchWire {
        rewrite * /search-the-wire.json
        file_server
      }
      handle @searchBB {
        rewrite * /search-breaking-bad.json
        file_server
      }
      handle @searchSev {
        rewrite * /search-severance.json
        file_server
      }
      handle @searchDark {
        rewrite * /search-dark.json
        file_server
      }

      handle /shows/1/seasons {
        rewrite * /seasons-1.json
        file_server
      }
      handle /shows/2/seasons {
        rewrite * /seasons-2.json
        file_server
      }
      handle /shows/3/seasons {
        rewrite * /seasons-3.json
        file_server
      }
      handle /shows/4/seasons {
        rewrite * /seasons-4.json
        file_server
      }
    }
  '';
in
{
  name = "temporal-jellyfin";

  nodes = {

    jellyfin =
      { pkgs, ... }:
      {
        networking.firewall.allowedTCPPorts = [ 8096 ];

        services.jellyfin.enable = true;

        environment.systemPackages = with pkgs; [
          curl
          ffmpeg
          jq
        ];

        virtualisation = {
          diskSize = 4 * 1024;
          memorySize = 2 * 1024;
        };
      };

    llm =
      { ... }:
      {
        networking.firewall.allowedTCPPorts = [ 8080 ];

        services.llama-cpp = {
          enable = true;
          settings = {
            model = model;
            host = "0.0.0.0";
            port = 8080;
          };
        };

        virtualisation = {
          memorySize = 4 * 1024;
          cores = 2;
        };
      };

    temporal =
      { pkgs, ... }:
      {
        networking.firewall.allowedTCPPorts = [ 7233 ];

        environment.systemPackages = [
          pkgs.grpc-health-probe
          (pkgs.temporal-cli.overrideAttrs (_: rec {
            version = "1.3.0";
            src = pkgs.fetchFromGitHub {
              owner = "temporalio";
              repo = "cli";
              tag = "v${version}";
              hash = "sha256-9O+INXJhNwgwwvC0751ifdHmxbD0qI5A3LdDb4Krk/o=";
            };
            vendorHash = "sha256-Xe/qrlqg6DpCNmsO/liTKjWIaY3KznkOQdXSSoJVZq4=";
            doCheck = false;
          }))
        ];

        services.temporal = {
          enable = true;
          settings = {
            log = {
              stdout = true;
              level = "info";
            };
            services = {
              frontend.rpc = {
                grpcPort = 7233;
                membershipPort = 6933;
                bindOnIP = "0.0.0.0";
                httpPort = 7243;
              };
              matching.rpc = {
                grpcPort = 7235;
                membershipPort = 6935;
                bindOnLocalHost = true;
              };
              history.rpc = {
                grpcPort = 7234;
                membershipPort = 6934;
                bindOnLocalHost = true;
              };
              worker.rpc = {
                grpcPort = 7239;
                membershipPort = 6939;
                bindOnLocalHost = true;
              };
            };
            global.membership = {
              maxJoinDuration = "30s";
              broadcastAddress = "0.0.0.0";
            };
            persistence = {
              defaultStore = "sqlite-default";
              visibilityStore = "sqlite-visibility";
              numHistoryShards = 1;
              datastores = {
                sqlite-default.sql = {
                  pluginName = "sqlite";
                  databaseName = "default";
                  connectAddr = "localhost";
                  connectProtocol = "tcp";
                  connectAttributes = {
                    mode = "memory";
                    cache = "private";
                  };
                  maxConns = 1;
                  maxIdleConns = 1;
                  maxConnLifetime = "1h";
                };
                sqlite-visibility.sql = {
                  pluginName = "sqlite";
                  databaseName = "default";
                  connectAddr = "localhost";
                  connectProtocol = "tcp";
                  connectAttributes = {
                    mode = "memory";
                    cache = "private";
                  };
                  maxConns = 1;
                  maxIdleConns = 1;
                  maxConnLifetime = "1h";
                };
              };
            };
            clusterMetadata = {
              enableGlobalNamespace = false;
              failoverVersionIncrement = 10;
              masterClusterName = "active";
              currentClusterName = "active";
              clusterInformation.active = {
                enabled = true;
                initialFailoverVersion = 1;
                rpcName = "frontend";
                rpcAddress = "temporal:7233";
                httpAddress = "temporal:7243";
              };
            };
            dcRedirectionPolicy.policy = "noop";
          };
        };

        virtualisation.cores = 2;
      };

    mock-api =
      { ... }:
      {
        networking.firewall.allowedTCPPorts = [ 8081 ];

        services.caddy = {
          enable = true;
          configFile = tvmazeMockConfig;
        };
      };

    worker =
      { pkgs, lib, ... }:
      {
        environment.systemPackages = [
          (pkgs.temporal-cli.overrideAttrs (_: rec {
            version = "1.3.0";
            src = pkgs.fetchFromGitHub {
              owner = "temporalio";
              repo = "cli";
              tag = "v${version}";
              hash = "sha256-9O+INXJhNwgwwvC0751ifdHmxbD0qI5A3LdDb4Krk/o=";
            };
            vendorHash = "sha256-Xe/qrlqg6DpCNmsO/liTKjWIaY3KznkOQdXSSoJVZq4=";
            doCheck = false;
          }))
        ];

        systemd.services.jellyfin-recommender = {
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          unitConfig.ConditionPathExists = "/etc/jellyfin-recommender/env";
          serviceConfig = {
            ExecStart = "${lib.getExe' recommendationsWorkerApp "recommender-worker.py"}";
            EnvironmentFile = "/etc/jellyfin-recommender/env";
            Restart = "on-failure";
            RestartSec = "2s";
            DynamicUser = true;
          };
        };

        systemd.services.jellyfin-missing-seasons = {
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          unitConfig.ConditionPathExists = "/etc/jellyfin-missing-seasons/env";
          serviceConfig = {
            ExecStart = "${lib.getExe' missingSeasonsWorkerApp "missing-seasons-worker.py"}";
            EnvironmentFile = "/etc/jellyfin-missing-seasons/env";
            Restart = "on-failure";
            RestartSec = "2s";
            DynamicUser = true;
          };
        };
      };

  };

  testScript =
    let
      jellyfinPost =
        path: jsonFile:
        "curl --fail -s -X POST 'http://jellyfin:8096${path}'"
        + " -H 'Content-Type:application/json'"
        + " -H 'X-Emby-Authorization:${authHeader}'"
        + " -d '@${jsonFile}'";
    in
    ''
      import json
      from urllib.parse import urlencode

      jellyfin.start()
      jellyfin.wait_for_unit("jellyfin.service")
      jellyfin.wait_for_open_port(8096)
      jellyfin.wait_until_succeeds(
          "journalctl --since -1m --unit jellyfin --grep 'Startup complete'"
      )

      with jellyfin.nested("Complete startup wizard"):
          jellyfin.wait_until_succeeds(
              "curl --fail -s 'http://jellyfin:8096/Startup/Configuration'"
              """ -H 'X-Emby-Authorization:${authHeader}'"""
          )
          jellyfin.succeed(
              "curl --fail -s 'http://jellyfin:8096/Startup/FirstUser'"
              """ -H 'X-Emby-Authorization:${authHeader}'"""
          )
          jellyfin.succeed("""${jellyfinPost "/Startup/Complete" emptyPayload}""")

      with jellyfin.nested("Authenticate as admin"):
          auth_result = json.loads(
              jellyfin.succeed("""${jellyfinPost "/Users/AuthenticateByName" jellyfinSetupPayload}""")
          )
          auth_token = auth_result["AccessToken"]
          user_id = auth_result["User"]["Id"]

      token_header = f'X-Emby-Authorization:${authHeader}, Token={auth_token}'

      def api_get(path):
          return f"curl --fail -s 'http://jellyfin:8096{path}' -H '{token_header}'"

      def api_post(path, data="{}"):
          return (
              f"curl --fail -s -X POST 'http://jellyfin:8096{path}'"
              f" -H 'Content-Type:application/json'"
              f" -H '{token_header}'"
              f" -d '{data}'"
          )

      with jellyfin.nested("Create movie library with fake films"):
          movie_dir = jellyfin.succeed("mktemp -d -p /var/lib/jellyfin").strip()
          jellyfin.succeed(f"chmod 755 '{movie_dir}'")

          # (title, watched, favorite)
          movies = [
              ("Blade Runner 2049 (2017)", True,  True),
              ("Arrival (2016)",           True,  True),
              ("Bāhubali: The Beginning (2015)", True,  True),
              ("Dune (2021)",              True,  False),
              ("Interstellar (2014)",      True,  False),
              ("Annihilation (2018)",      False, False),
              ("Ex Machina (2014)",        False, False),
          ]

          for title, _watched, _fav in movies:
              jellyfin.succeed(
                  f"ffmpeg -f lavfi -i testsrc2=duration=1 '{movie_dir}/{title}.mkv' -y"
              )

          add_movie_lib = urlencode({
              "name":           "Movies",
              "collectionType": "movies",
              "paths":          movie_dir,
              "refreshLibrary": "true",
          })
          jellyfin.succeed(api_post(f"/Library/VirtualFolders?{add_movie_lib}"))

      with jellyfin.nested("Create TV series library with fake shows"):
          show_dir = jellyfin.succeed("mktemp -d -p /var/lib/jellyfin").strip()
          jellyfin.succeed(f"chmod 755 '{show_dir}'")

          # (title, watched, favorite)
          series = [
              ("The Wire (2002)",     True,  True),
              ("Breaking Bad (2008)", True,  False),
              ("Severance (2022)",    False, False),
              ("Dark (2017)",         False, False),
          ]

          for title, _watched, _fav in series:
              base = title.rsplit(" (", 1)[0]
              ep_dir = f"{show_dir}/{title}/Season 01"
              jellyfin.succeed(f"mkdir -p '{ep_dir}'")
              jellyfin.succeed(
                  f"ffmpeg -f lavfi -i testsrc2=duration=1 '{ep_dir}/{base} S01E01.mkv' -y"
              )

          add_show_lib = urlencode({
              "name":           "TV Shows",
              "collectionType": "tvshows",
              "paths":          show_dir,
              "refreshLibrary": "true",
          })
          jellyfin.succeed(api_post(f"/Library/VirtualFolders?{add_show_lib}"))

      def library_idle(_):
          folders = json.loads(jellyfin.succeed(api_get("/Library/VirtualFolders")))
          return all(f.get("RefreshStatus") == "Idle" for f in folders)

      retry(library_idle)

      with jellyfin.nested("Wait for all movies to appear"):
          movie_items = []

          def has_all_movies(_):
              global movie_items
              result = json.loads(
                  jellyfin.succeed(
                      api_get(f"/Users/{user_id}/Items?IncludeItemTypes=Movie&Recursive=true")
                  )
              )
              movie_items = result["Items"]
              return len(movie_items) == len(movies)

          retry(has_all_movies)

      with jellyfin.nested("Wait for all series to appear"):
          series_items = []

          def has_all_series(_):
              global series_items
              result = json.loads(
                  jellyfin.succeed(
                      api_get(f"/Users/{user_id}/Items?IncludeItemTypes=Series&Recursive=true")
                  )
              )
              series_items = result["Items"]
              return len(series_items) == len(series)

          retry(has_all_series)

      with jellyfin.nested("Mark watched and favorite films"):
          name_to_id = {item["Name"]: item["Id"] for item in movie_items}

          for title, watched, fav in movies:
              base = title.rsplit(" (", 1)[0]
              item_id = next(
                  (v for k, v in name_to_id.items() if base in k), None
              )
              if item_id is None:
                  raise Exception(
                      f"Could not find Jellyfin item for '{title}' in {list(name_to_id)}"
                  )
              if watched:
                  jellyfin.succeed(api_post(f"/Users/{user_id}/PlayedItems/{item_id}"))
              if fav:
                  jellyfin.succeed(api_post(f"/Users/{user_id}/FavoriteItems/{item_id}"))

      with jellyfin.nested("Mark watched and favorite series"):
          name_to_id = {item["Name"]: item["Id"] for item in series_items}

          for title, watched, fav in series:
              base = title.rsplit(" (", 1)[0]
              item_id = next(
                  (v for k, v in name_to_id.items() if base in k), None
              )
              if item_id is None:
                  raise Exception(
                      f"Could not find Jellyfin item for '{title}' in {list(name_to_id)}"
                  )
              if watched:
                  episodes = json.loads(
                      jellyfin.succeed(
                          api_get(
                              f"/Users/{user_id}/Items"
                              f"?ParentId={item_id}&IncludeItemTypes=Episode&Recursive=true"
                          )
                      )
                  )
                  for ep in episodes.get("Items", []):
                      jellyfin.succeed(api_post(f"/Users/{user_id}/PlayedItems/{ep['Id']}"))
              if fav:
                  jellyfin.succeed(api_post(f"/Users/{user_id}/FavoriteItems/{item_id}"))

      llm.start()
      llm.wait_for_unit("llama-cpp.service")
      llm.wait_for_open_port(8080)
      llm.wait_until_succeeds("curl --fail -s http://localhost:8080/health")

      mock_api.start()
      mock_api.wait_for_unit("caddy.service")
      mock_api.wait_for_open_port(8081)

      temporal.start()
      temporal.wait_for_unit("temporal.service")
      temporal.wait_for_open_port(7233)
      temporal.wait_until_succeeds(
          "grpc-health-probe -addr=127.0.0.1:7233"
          " -service=temporal.api.workflowservice.v1.WorkflowService"
      )
      temporal.wait_until_succeeds(
          "journalctl -o cat -u temporal.service | grep 'Frontend is now healthy'"
      )
      temporal.log(
          temporal.wait_until_succeeds(
              "temporal operator namespace create --namespace jellyfin --address 127.0.0.1:7233",
              timeout=60,
          )
      )

      worker.start()
      worker.systemctl("start network-online.target")
      worker.wait_for_unit("network-online.target")

      common_env = f"""\
      JELLYFIN_URL=http://jellyfin:8096
      JELLYFIN_API_KEY={auth_token}
      JELLYFIN_USER_ID={user_id}
      OPENAI_BASE_URL=http://llm:8080/v1
      OPENAI_API_KEY=not-needed
      RECOMMENDER_MODEL=gemma4:e2b
      TEMPORAL_ADDRESS=temporal:7233
      TEMPORAL_NAMESPACE=jellyfin
      """

      worker.succeed(f"""
          mkdir -p /etc/jellyfin-recommender
          cat > /etc/jellyfin-recommender/env <<'ENVEOF'
      {common_env}TEMPORAL_TASK_QUEUE=recommendations-queue
      ENVEOF
      """)

      worker.succeed(f"""
          mkdir -p /etc/jellyfin-missing-seasons
          cat > /etc/jellyfin-missing-seasons/env <<'ENVEOF'
      {common_env}TEMPORAL_TASK_QUEUE=missing-seasons-queue
      TVMAZE_BASE_URL=http://mock-api:8081
      ENVEOF
      """)

      worker.systemctl("start jellyfin-recommender.service")
      worker.wait_for_unit("jellyfin-recommender.service")

      worker.systemctl("start jellyfin-missing-seasons.service")
      worker.wait_for_unit("jellyfin-missing-seasons.service")

      with temporal.nested("Run RecommendationsWorkflow"):
          temporal.succeed(
              "temporal workflow start"
              " --namespace jellyfin"
              " --address 127.0.0.1:7233"
              " --type RecommendationsWorkflow"
              " --task-queue recommendations-queue"
              " --workflow-id jellyfin-rec-test"
          )

          result_json = json.loads(
              temporal.wait_until_succeeds(
                  "temporal workflow result"
                  " --namespace jellyfin"
                  " --address 127.0.0.1:7233"
                  " --workflow-id jellyfin-rec-test"
                  " --output json",
                  timeout=300,
              )
          )

          assert result_json["status"] == "COMPLETED", (
              f"RecommendationsWorkflow did not complete: {result_json}"
          )
          assert result_json["result"], "RecommendationsWorkflow returned empty result"
          temporal.log(f"Recommendations:\n{result_json['result']}")

      with temporal.nested("Run MissingSeasonsWorkflow"):
          temporal.succeed(
              "temporal workflow start"
              " --namespace jellyfin"
              " --address 127.0.0.1:7233"
              " --type MissingSeasonsWorkflow"
              " --task-queue missing-seasons-queue"
              " --workflow-id missing-seasons-test"
          )

          result_json = json.loads(
              temporal.wait_until_succeeds(
                  "temporal workflow result"
                  " --namespace jellyfin"
                  " --address 127.0.0.1:7233"
                  " --workflow-id missing-seasons-test"
                  " --output json",
                  timeout=300,
              )
          )

          assert result_json["status"] == "COMPLETED", (
              f"MissingSeasonsWorkflow did not complete: {result_json}"
          )
          assert result_json["result"], "MissingSeasonsWorkflow returned empty result"
          temporal.log(f"Missing seasons report:\n{result_json['result']}")
    '';
}
