{
  description = "AudioBookRequest";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils = {
      url = "github:numtide/flake-utils";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

        pyprojectOverrides = final: prev: {
          # both fastapi and fastapi-cli add the binary causing "FileCollisionError: Two or more packages are trying to provide the same file with different contents"
          fastapi-cli = prev.fastapi-cli.overrideAttrs (_: {
            postInstall = "rm $out/bin/fastapi";
          });
        };

        overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
          pkgs.lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]
        );
      in
      rec {
        packages = rec {
          # Creates a separate nix store virtualenv with the default dependencies (no devDependencies)
          default = pythonSet.mkVirtualEnv "audiobookrequest-venv" workspace.deps.default;

          docker =
            let
              npmDeps = pkgs.importNpmLock.buildNodeModules {
                package = pkgs.lib.importJSON ./package.json;
                packageLock = pkgs.lib.importJSON ./package-lock.json;
                nodejs = pkgs.nodejs_24;
              };
              tw-init = pkgs.writeShellScriptBin "tw-init" ''
                ln -s ${npmDeps}/node_modules node_modules
                cp -r ${gitignore}/templates templates # copy over to make sure tailwind generates the correct classes
                cp ${./static/tw.css} tw.css # copy over the file since tailwind looks for daisyui relative to the input file
                ${pkgs.tailwindcss_4}/bin/tailwindcss -i tw.css -o $out/app/static/globals.css -m
              '';
              run = pkgs.writeShellScriptBin "run" ''
                ${default}/bin/alembic upgrade heads
                # exec is important to allow for C-c to work
                exec ${default}/bin/fastapi run --port $ABR_APP__PORT
              '';
              gitignore = pkgs.nix-gitignore.gitignoreSource [ ] ./.;
              htmx-preload = builtins.fetchurl {
                url = "https://unpkg.com/htmx-ext-preload@2.1.0/preload.js";
                sha256 = "sha256:1bfkr60i20aj16vbwz2nv1q5fmmmzmc52i2aqn5cx6xihbmwy7nd";
              };
              htmx = builtins.fetchurl {
                url = "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js";
                sha256 = "sha256:0ixlixv36rrfzj97g2w0q6jxbg0x1rswgvvd2vrpjm13r2jxs2g2";
              };
              alpinejs = builtins.fetchurl {
                url = "https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js";
                sha256 = "sha256:1lqa3v5p7pwz3599xnxf5bwxf17bbmqxcqz3cpgj32a8ab9fxl9y";
              };
              toastifyjs = builtins.fetchurl {
                url = "https://cdn.jsdelivr.net/npm/toastify-js";
                sha256 = "sha256:06mcmh3gl8mv5c4js767crpz010wrf8hyk47cm1i3hbcf9j195n2";
              };
              toastifycss = builtins.fetchurl {
                url = "https://cdn.jsdelivr.net/npm/toastify-js/src/toastify.min.css";
                sha256 = "sha256:1rgc3cx546vzkyzb3z5g7bpkdvlma7r0jwhp9mnx20f3rhfl7lwb";
              };
            in

            pkgs.dockerTools.buildImage {
              name = "audiobookrequest";

              copyToRoot = pkgs.buildEnv {
                name = "test";
                paths = [ ];
                postBuild = ''
                  mkdir -p $out/app/static
                  ${tw-init}/bin/tw-init
                  cp ${gitignore}/alembic.ini $out/app/alembic.ini
                  cp -r ${gitignore}/alembic $out/app/alembic
                  cp -r ${gitignore}/templates $out/app/templates
                  cp -r ${gitignore}/static/* $out/app/static
                  cp -r ${gitignore}/app $out/app/app

                  cp ${htmx-preload} $out/app/static/htmx-preload.js
                  cp ${htmx} $out/app/static/htmx.js
                  cp ${alpinejs} $out/app/static/alpine.js
                  cp ${toastifyjs} $out/app/static/toastify.js
                  cp ${toastifycss} $out/app/static/toastify.css
                '';
              };

              config = {
                WorkingDir = "/app";
                Cmd = [ "${run}/bin/run" ];
                ExposedPorts = {
                  "8000/tcp" = { };
                };
                Env = [
                  "ABR_APP__PORT=8000"
                  "ABR_APP__VERSION=${builtins.readFile ./static/version}"
                ];
              };
            };
        };

        # What is run when we use `nix run . -- dev`
        apps.default = {
          type = "app";
          program = "${packages.default}/bin/fastapi";
        };

        # Create a .venv and activates it. Allows for the venv to easily be selected in the editor for the python interpreter
        devShells.default = pkgs.mkShell {
          venvDir = ".venv";
          packages = with pkgs; [
            nodejs_24
            sqlite
            nodePackages.browser-sync
            python312Packages.venvShellHook
            uv
            hugo
          ];
        };
      }
    );
}
