# Environments: where commands run

By default the `bash` and `shell` tools run commands on your machine, in the project folder. An
environment runs them somewhere else: a Docker or Podman container, another machine over SSH, a
Singularity/Apptainer image, or any wrapper command you like (Modal, Daytona, a VM…).

Only shell commands move. The file tools (`read`, `write`, `edit`, `glob`, `grep`) always work on the
local files.

## Switching

```
/env                         show the current environment
/env local                   back to your machine
/env docker python:3.12      a container (also: /env podman <image>)
/env ssh user@box /srv/app   another machine
/env apptainer env.sif       an Apptainer or Singularity image
/env custom daytona exec ws -- sh -c {command}
```

`/env` changes it for this session. To keep it, set `"environment"` in the config
(`~/.nanocode/config.json`).

## Docker and Podman

```json
"environment": {"type": "docker", "image": "python:3.12"}
```

nanocode starts one long-lived container (`docker run -d … sleep infinity`) the first time a command
runs, with the project folder mounted at the same path (on Windows, at `/workspace`). Each command
then runs with `docker exec -w <current folder>`, so the container sees the same files as the file
tools. When nanocode closes it removes the container. Commands that run at the same time wait for
the first one to start the container, so only one is started.

On Windows the wrapped command goes through `cmd.exe`: nanocode escapes every `cmd.exe` special
character (`^ & | < > ( ) % !` and quotes) so the command reaches Docker, SSH or the wrapper exactly
as written, and sends a multi-line command base64-encoded (`eval "$(echo … | base64 -d)"`), since
`cmd.exe` can't carry a line break. The far side needs `base64` for that (most images have it).

Options:

- `"engine": "podman"` uses Podman instead of Docker.
- `"container": "mybox"` uses that name. If a container with that name exists it's reused (and
  started if it's stopped); at shutdown it's stopped, not removed, so installed packages stay.
- `"mount": false` doesn't mount the project; commands run in `"dir"` (default `/`).
- `"run_options": ["--network", "host"]` adds options to `docker run`.

## SSH

```json
"environment": {"type": "ssh", "host": "user@box", "dir": "/srv/app", "options": ["-p", "2222"]}
```

Each command runs as `ssh user@box 'cd /srv/app && <command>'`. SSH must log in without a
password prompt (use a key or an agent).

The file tools still read and edit your local files, which the remote machine doesn't see. This suits
running commands remotely (tests on a GPU box, logs, deploys). To work on remote files, either sync
them yourself (rsync, a shared folder) or ask nanocode to use `bash` (`cat`, `sed`, …) for them.

## Singularity and Apptainer

```json
"environment": {"type": "apptainer", "image": "env.sif"}
```

Commands run as `apptainer exec --pwd <folder> env.sif sh -c <command>`. Use `"type": "singularity"`
for the older name. Your home and current folder are mounted by default, so files are shared.

## Anything else

```json
"environment": {"type": "custom", "wrap": "daytona exec myws -- sh -c {command}"}
```

`{command}` is replaced by the command, quoted for your shell; `{cwd}` by the current folder. Use this
for Modal, Daytona, a VM, `nix develop -c sh -c {command}`, and so on.

If an environment can't run a command (no image set, Docker not running), the command fails with the
reason. It never falls back to running on your machine.
