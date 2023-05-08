#!/home/mila/n/normandf/.conda/envs/datamodules/bin/python3
"""Sets up a user cache directory for commonly used libraries, while reusing shared cache entries.

Use this to avoid having to download files to the $HOME directory, as well as to remove
duplicated downloads and free up space in your $HOME and $SCRATCH directories.

The user cache directory should be writeable, and doesn't need to be empty.
This command adds symlinks to (some of) the files contained in the *shared* cache directory to this
user cache directory.

The shared cache directory should be readable (e.g. a directory containing frequently-downloaded
weights/checkpoints, managed by the IT/IDT Team at Mila).

TODO:
This command also sets the environment variables via a block in the `$HOME/.bashrc` file, so that
these libraries look in the specified user cache for these files.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from logging import getLogger as get_logger
from pathlib import Path

logger = get_logger(__name__)

# TODO: The log messages don't appear unless we have the rich logging handler setup.

try:
    import rich.logging

    logger.addHandler(rich.logging.RichHandler(rich_tracebacks=True))
    RICH_LOGGING = True
except ImportError:
    RICH_LOGGING = False

logger.setLevel(logging.INFO)

SCRATCH = Path(os.environ["SCRATCH"])
DEFAULT_USER_CACHE_DIR = SCRATCH / "cache"
DEFAULT_SHARED_CACHE_DIR = Path("/network/weights/.shared_cache")


@dataclass
class Options:
    """Options for the setup_cache command."""

    user_cache_dir: Path = DEFAULT_USER_CACHE_DIR
    """The user cache directory.

    Should probably be in $SCRATCH (not $HOME!)
    """

    shared_cache_dir: Path = DEFAULT_SHARED_CACHE_DIR
    """The path to the shared cache directory.

    This defaults to the path of the shared cache setup by the IDT team on the Mila cluster.
    """


def main(argv: list[str] | None = None):
    options = _parse_args(argv)
    setup_cache(user_cache_dir=options.user_cache_dir, shared_cache_dir=options.shared_cache_dir)


def _parse_args(argv: list[str] | None) -> Options:
    try:
        from simple_parsing import ArgumentParser

        parser = ArgumentParser(description=__doc__)
        parser.add_arguments(Options, dest="options")
        args = parser.parse_args()

        options: Options = args.options
    except ImportError:
        from argparse import ArgumentParser

        parser = ArgumentParser(description=__doc__)
        parser.add_argument(
            "--user_cache_dir",
            type=Path,
            default=DEFAULT_USER_CACHE_DIR,
            help="The user cache directory. Should probably be in $SCRATCH (not $HOME!)",
        )
        parser.add_argument(
            "--shared_cache_dir",
            type=Path,
            default=DEFAULT_SHARED_CACHE_DIR,
            help=(
                "The shared cache directory. This defaults to the path of the shared cache setup "
                "by the IDT team on the Mila cluster."
            ),
        )
        args = parser.parse_args(argv)
        user_cache_dir: Path = args.user_cache_dir
        shared_cache_dir: Path = args.shared_cache_dir
        options = Options(
            user_cache_dir=user_cache_dir,
            shared_cache_dir=shared_cache_dir,
        )
    return options


def setup_cache(user_cache_dir: Path, shared_cache_dir: Path) -> None:
    """Set up the user cache directory.

    1. If the user cache directory doesn't exist, creates it.
    2. Sets the optimal striping configuration for the user cache directory so that reading and
       writing large files works optimally.
    3. Removes broken symlinks in the user cache directory if they point to files in
       `shared_cache_dir` that don't exist anymore.
    4. For every file in the shared cache dir, creates a symbolic link to it in the
       user cache dir. (Replaces duplicate downloaded files with symlinks to the same file in the
       shared cache dir).
    5. Adds a block of code to ~/.bash_aliases (creating it if necessary) that sets the relevant
       environment variables so that libraries use those cache directories.
    """

    if not user_cache_dir.exists():
        user_cache_dir.mkdir(parents=True, exist_ok=False)
    if not user_cache_dir.is_dir():
        raise RuntimeError(f"cache_dir is not a directory: {user_cache_dir}")
    if not shared_cache_dir.is_dir():
        raise RuntimeError(
            f"The shared cache directory {shared_cache_dir} doesn't exist, or isn't a directory! "
        )

    set_striping_config_for_dir(user_cache_dir)
    delete_broken_symlinks_to_shared_cache(user_cache_dir, shared_cache_dir)
    create_links(user_cache_dir, shared_cache_dir)
    bash_aliases_file = "~/.bash_aliases"
    bash_aliases_file_changed = set_environment_variables(
        user_cache_dir, bash_aliases_file=bash_aliases_file
    )
    if bash_aliases_file_changed:
        logger.warning(
            f"The {bash_aliases_file} was changed. You may need to restart your shell for the "
            f"changes to take effect.\n"
            f"To set the environment variables in the current shell, source the "
            f"{bash_aliases_file} file like so:\n"
            f"```\n"
            f"source {bash_aliases_file}\n"
            f"```\n"
        )

    print("DONE!")


def set_striping_config_for_dir(dir: Path, num_targets: int = 4, chunksize: str = "512k"):
    """Sets up the data striping configuration for the given directory using beegfs-ctl.

    This is useful when we know that the directory will contain large files, since we can stripe
    the data in chunks across multiple targets, which will improve performance.

    NOTE: This only affects the files that are added in this directory *after* this command is ran.
    """
    output = subprocess.check_output(
        shlex.split(
            f"beegfs-ctl --cfgFile=/etc/beegfs/scratch.d/beegfs-client.conf --setpattern "
            f"--numtargets={num_targets} --chunksize={chunksize} {dir}"
        ),
        encoding="utf-8",
    )
    logger.info(f"Set the striping config for {dir}")
    logger.info(output)


def set_environment_variables(
    user_cache_dir: Path, bash_aliases_file: str | Path = "~/.bash_aliases"
) -> bool:
    """Adds a block of code to the bash_aliases file (creating it if necessary) that sets some
    relevant environment variables for each library so they start to use the new cache dir.

    Also sets the environment variables in `os.environ` so the current process gets them. Returns
    whether the contents of the bash aliases file was changed.
    """
    # TODO: These changes won't persist. We probably need to add a block of code in .bashrc
    bash_aliases_file = Path(bash_aliases_file).expanduser().resolve()
    file = bash_aliases_file

    env_vars = {
        "HF_HOME": user_cache_dir / "huggingface",
        "HF_DATASETS_CACHE": user_cache_dir / "huggingface" / "datasets",
        "TRANSFORMERS_CACHE": user_cache_dir / "huggingface" / "transformers",
        "TORCH_HOME": user_cache_dir / "torch",
    }

    for key, value in env_vars.items():
        os.environ[key] = str(value)

    start_flag = "# >>> cache setup >>>"
    end_flag = "# <<< cache setup <<<"
    lines_to_add = [
        start_flag,
        *(f"export {var_name}={str(var_value)}" for var_name, var_value in env_vars.items()),
        end_flag,
    ]

    if not file.exists():
        file.touch(0o644)
        file.write_text("#!/bin/bash\n")

    with open(file, "r") as f:
        lines = f.readlines()
    block_of_text = "\n".join(lines_to_add) + "\n"
    start_line = start_flag + "\n"
    end_line = end_flag + "\n"

    _update_start_and_end_flags(file, start_line, end_line)

    if start_line not in lines and end_line not in lines:
        logger.info(f"Adding a block of text at the bottom of {file}:")
        with open(file, "a") as f:
            for line in block_of_text.splitlines():
                logger.debug(line.strip())
            f.write("\n\n" + block_of_text + "\n")
        return True

    if start_line in lines and end_line in lines:
        logger.debug(f"Block is already present in {file}.")

        start_index = lines.index(start_line)
        end_index = lines.index(end_line)

        if all(
            line.strip() == lines_to_add[i]
            for i, line in enumerate(lines[start_index : end_index + 1])
        ):
            logger.debug("Block has same contents.")
            return False
        else:
            logger.debug("Updating the context of the block:")
            new_lines = block_of_text.splitlines(keepends=True)
            lines[start_index : end_index + 1] = new_lines
            with open(file, "w") as f:
                for line in new_lines:
                    logger.debug(line.strip())
                f.writelines(lines)
                if len(lines) == end_index + 1:
                    # Add an empty line at the end.
                    f.write("\n")

            return True

    logger.error(
        f"Weird! The block of text is only partially present in {file}! (unable to find both "
        f"the start and end flags). Doing nothing. \n"
        f"Consider fixing the {file} file manually and re-running the command, or letting IDT "
        f"know."
    )
    return False


def _update_start_and_end_flags(file: Path, start_flag: str, end_flag: str) -> bool:
    """Replaces old versions of the start and end flags with the new ones, if they exist.

    Returns whether the file was modified.
    """
    # TODO: Keep a list of the previous block flags if we end up changing either the start or end,
    # so we can identify and replace old blocks too.
    previous_start_flags = []
    previous_end_flags = []
    update_start_and_end_flags = False

    with open(file, "r") as f:
        lines = f.readlines()

    start_line = start_flag + "\n"
    end_line = end_flag + "\n"

    for previous_flag in previous_start_flags:
        if previous_flag + "\n" in lines:
            index = lines.index(previous_flag + "\n")
            lines[index] = start_line
            update_start_and_end_flags = True

    for previous_flag in previous_end_flags:
        if previous_flag + "\n" in lines:
            index = lines.index(previous_flag + "\n")
            lines[index] = end_line
            update_start_and_end_flags = True

    if update_start_and_end_flags:
        with open(file, "w") as f:
            logger.info("Replacing old start and end flags with the new ones.")
            f.writelines(lines)
            return True
    return False


def _is_child(path: Path, parent: Path) -> bool:
    """Return True if the path is under the parent directory."""
    if path == parent:
        return False
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def delete_broken_symlinks_to_shared_cache(user_cache_dir: Path, shared_cache_dir: Path):
    """Delete all symlinks in the user cache directory that point to files that don't exist anymore
    in the shared cache directory."""
    for file in user_cache_dir.rglob("*"):
        if file.is_symlink():
            target = file.resolve()
            if _is_child(target, shared_cache_dir) and not target.exists():
                logger.debug(f"Removing broken symlink: {file}")
                if file.is_dir():
                    file.rmdir()
                else:
                    file.unlink()


def create_links(user_cache_dir: Path, shared_cache_dir: Path):
    """Create symlinks to the shared cache directory in the user cache directory."""
    # For every file in the shared cache dir, create a (symbolic?) link to it in the user cache dir

    # TODO: Using `shutil.copytree` raises a bunch of errors at the end. I'm not sure why.
    # Using the more direct method below instead. One disadvantage is that we can't really use
    # `shutil.ignore_dirs` which would be useful to ignore some directories in the shared cache.

    # shutil.copytree(
    #     shared_cache_dir,
    #     user_cache_dir,
    #     symlinks=True,
    #     copy_function=_custom_copy_fn,
    #     dirs_exist_ok=True,
    # )

    for path_in_shared_cache in shared_cache_dir.rglob("*"):
        relative_path = path_in_shared_cache.relative_to(shared_cache_dir)
        path_in_user_cache = user_cache_dir / relative_path

        if path_in_shared_cache.is_dir():
            path_in_user_cache.mkdir(exist_ok=True)
            continue

        if not path_in_user_cache.exists():
            logger.info(f"Creating a new symlink at {path_in_user_cache}")
            path_in_user_cache.symlink_to(path_in_shared_cache)
            continue

        if not path_in_user_cache.is_symlink():
            logger.info(
                f"Replacing duplicate file {path_in_user_cache} with a symlink to the "
                f"same file in the shared cache."
            )
            path_in_user_cache.unlink()
            path_in_user_cache.symlink_to(path_in_shared_cache)
            continue

        # The file in the user cache is a symlink.
        user_cache_file_target = path_in_user_cache.resolve()
        if user_cache_file_target == path_in_shared_cache:
            # Symlink from a previous run, nothing to do.
            logger.debug(f"Symlink from a previous run at {path_in_user_cache}")
            continue

        if not user_cache_file_target.exists():
            # broken symlink (perhaps from a previous run?)
            logger.warning(f"Removing broken symlink: {path_in_user_cache}")
            path_in_user_cache.unlink()
            path_in_user_cache.symlink_to(path_in_shared_cache)
            continue

        if path_in_shared_cache.is_symlink():
            path_in_shared_cache_target = path_in_shared_cache.resolve()
            if path_in_shared_cache_target.exists():
                logger.debug(f"Making a symlink to a symlink at {path_in_user_cache}.")
                path_in_user_cache.unlink()
                path_in_user_cache.symlink_to(path_in_shared_cache)
                continue
            # Shared cache has a broken symlink!
            logger.error(
                f"Broken symlink in shared cache at "
                f"{path_in_shared_cache} -> {path_in_shared_cache_target}"
            )
            continue

        # Symlink that points to a different file? Do nothing in this case.
        logger.warning(
            f"Found a Weird symlink at {path_in_user_cache} that doesn't point to "
            f"{path_in_shared_cache}. (points to {user_cache_file_target} instead?) "
            f"Leaving it as-is."
        )


if __name__ == "__main__":
    main()
