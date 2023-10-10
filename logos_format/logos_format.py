#!/usr/bin/env python3

import argparse
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional, TextIO

from clang_format import _get_executable as clang_format_get_exe
from path import Path, TempDir
from rich.console import Console
from rich.logging import RichHandler

from logos_format._version import __version__ as logos_format_version

LOG_FORMAT = "%(message)s"
logging.basicConfig(
    level=logging.WARNING,
    format=LOG_FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler(console=Console(stderr=True))],
)

program_name = "logos-format"

log = logging.getLogger(program_name)

# block level
# hook -> replace with @logosformathook with ; at the end
# end -> replace with @logosformatend with ; at the end
# property -> replace with @logosformatproperty with NO ; at the end. Special case for block level
# new -> replce with @logosformatnew with ; at the end
# group -> replace with @logosformatgroup with ; at the end
# subclass -> replace with @logosformatsubclass with ; at the end
# top level
# config -> replace with @logosformatconfig
# hookf -> replace with @logosformathookf
# ctor -> replace with @logosformatctor
# dtor -> replace with @logosformatdtor

# function level
# init -> replace with @logosformatinit
# c -> replace with @logosformatc
# orig -> replace with @logosformatorig
# log -> replace with @logosformatlog

specialFilterList: tuple[str] = ("%hook", "%end", "%new", "%group", "%subclass")
filterList: tuple[str] = (
    "%property",
    "%config",
    "%hookf",
    "%ctor",
    "%dtor",
    "%init",
    "%c",
    "%orig",
    "%log",
)


def compile_logos_token_pat(token: str) -> re.Pattern:
    return re.compile(rf"%({token[1:]})\b")


logos_special_filter_pats: dict[str, re.Pattern] = {
    token: compile_logos_token_pat(token) for token in specialFilterList
}
logos_filter_pats: dict[str, re.Pattern] = {
    token: compile_logos_token_pat(token) for token in filterList
}

logos_extensions: tuple[str] = (".x", ".xi", ".xm", ".xmi")
logos_extensions_to_normal_extensions: dict[str, str] = {
    ".x": ".m",
    ".xi": ".m",
    ".xm": ".mm",
    ".xmi": ".mm",
}

clang_format_path: Path = Path(clang_format_get_exe("clang-format"))


def logos_to_norm(logos_file: TextIO, norm_file: TextIO) -> None:
    for line in logos_file.readlines():
        for token in filterList:
            if token in line:
                line = re.sub(logos_filter_pats[token], r"@logosformat\1", line)
        for token in specialFilterList:
            if token in line:
                line = re.sub(logos_special_filter_pats[token], r"@logosformat\1", line) + ";"
        norm_file.write(line)


def norm_to_logos(norm_file: TextIO, logos_file: TextIO) -> None:
    for line in norm_file.readlines():
        if "@logosformat" in line:
            line = line.replace("@logosformat", "%")
            if any(token in line for token in specialFilterList):
                line = line.replace(";", "")
        logos_file.write(line)


def get_logos_path(arg: str) -> Optional[Path]:
    path = Path(arg)
    if path.isfile() and path.ext in logos_extensions:
        if not path.access(os.R_OK):
            raise PermissionError(f"Can't read Logos file '{path}'")
        if not path.access(os.W_OK):
            raise PermissionError(f"Can't write Logos file '{path}'")
        return path
    else:
        return None


class SaveableTempDir(TempDir):
    @staticmethod
    def __super_kwargs__(**kwargs):
        my_kwargs = ("save",)
        super_kwargs = {k: v for k, v in kwargs.items() if k not in my_kwargs}
        return super_kwargs

    def __new__(cls, *args, **kwargs):
        return super().__new__(cls, *args, **cls.__super_kwargs__(**kwargs))

    def __init__(self, *args, save=False, **kwargs) -> None:
        super().__init__(*args, **self.__super_kwargs__(**kwargs))
        self._save = save

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._save:
            super().__exit__(exc_type, exc_val, exc_tb)


def real_main(logos_format_args: argparse.Namespace, clang_format_args: list[str]) -> int:
    if logos_format_args.version:
        print(f"{program_name} version {logos_format_version}")
        print(
            subprocess.run([clang_format_path, "--version"], capture_output=True, text=True).stdout,
            end="",
        )
        return 0
    verbose: bool = logos_format_args.verbose_logos
    if verbose:
        log.setLevel(logging.INFO)
        log.info(f"{program_name}: verbose-logos mode enabled")
    save_temps: bool = logos_format_args.save_logos_temps
    in_place: bool = logos_format_args.in_place
    if in_place:
        log.info(f"{program_name} operating in in-place mode")
    else:
        log.info(f"{program_name} will output formatted code to stdout")
    # gotta make the tmp dir in cwd or else clang-format won't find our .clang-format
    with SaveableTempDir(prefix=f"{program_name}-tmp-", dir=Path.getcwd(), save=save_temps) as d:
        if save_temps or verbose:
            log.warning(f"Saving {program_name} temporary files in '{d}'")
            if save_temps:
                log.info(f"{program_name} will not delete the directory on exit")
        new_cf_args: list[str] = []
        tmp_norm_to_tmp_logos: dict[Path, Path] = {}
        tmp_logos_to_orig_logos: dict[Path, Path] = {}
        for cf_arg in clang_format_args:
            orig_logos = get_logos_path(cf_arg)
            # transform original Logos file to normal temp file
            if orig_logos is not None:
                log.info(f"Found a Logos file to format: '{orig_logos}'")
                norm_ext = logos_extensions_to_normal_extensions[orig_logos.ext]
                tmp_norm = Path(
                    tempfile.NamedTemporaryFile(
                        mode="w", dir=d, prefix=orig_logos.stem + "-", suffix=norm_ext, delete=False
                    ).name
                )
                log.info(f"Writing de-Logos'ed version of '{orig_logos}' at '{tmp_norm}'")
                if in_place:
                    # create a temporary Logos file for re-Logos'ing to avoid partial failure
                    tmp_logos = Path(
                        tempfile.NamedTemporaryFile(
                            mode="w",
                            dir=d,
                            prefix=orig_logos.stem + "-",
                            suffix=orig_logos.ext,
                            delete=False,
                        ).name
                    )
                    tmp_norm_to_tmp_logos[tmp_norm] = tmp_logos
                    tmp_logos_to_orig_logos[tmp_logos] = orig_logos
                log.info(f"Transforming '{orig_logos}' to temporary normalized '{tmp_norm}'")
                with open(orig_logos) as orig_logos_file, open(tmp_norm, "w") as tmp_norm_file:
                    logos_to_norm(orig_logos_file, tmp_norm_file)
                if save_temps and not in_place:
                    tmp_norm_unformatted = (
                        tmp_norm.parent / tmp_norm.stem + "-unformatted" + tmp_norm.ext
                    )
                    log.info(
                        f"Saving additional copy of clang-format protected version of '{orig_logos}' at '{tmp_norm_unformatted}'"
                    )
                    tmp_norm.copy(tmp_norm_unformatted)
                new_cf_args.append(tmp_norm)
            else:
                new_cf_args.append(cf_arg)
        # run clang-format
        if in_place:
            new_cf_args.insert(0, "-i")
        new_cf_cmd = [clang_format_path, *new_cf_args]
        log.info(f"{program_name} is running '{' '.join(new_cf_cmd)}'")
        try:
            cf_res = subprocess.run(
                new_cf_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            log.error(f"clang-format output:\n{e.output}")
            log.exception(
                f"{program_name} got return code {e.returncode} while running '{' '.join(new_cf_cmd)}'"
            )
            return e.returncode
        except Exception as e:
            log.error(f"clang-format output:\n{cf_res.stdout}")
            log.exception(f"Received an unexpected exception when running clang-format")
            return 1
        if in_place:
            log.info(f"{program_name} performing post-clang-format in-place post-processing")
            print(cf_res.stdout, end="")
            # re-Logos the clang-format'ed normalized file
            for tmp_norm, tmp_logos in tmp_norm_to_tmp_logos.items():
                log.info(
                    f"Transforming temporary normalized '{tmp_norm}' to temporary Logos '{tmp_logos}'"
                )
                with open(tmp_norm) as tmp_norm_file, open(tmp_logos, "w") as tmp_logos_file:
                    norm_to_logos(tmp_norm_file, tmp_logos_file)
            # Copy changes from temporary clang-format'ed Logos file to original Logos file
            for tmp_logos, orig_logos in tmp_logos_to_orig_logos.items():
                log.info(f"Overwriting '{orig_logos}' with content from '{tmp_logos}'")
                with open(tmp_logos) as tl, open(orig_logos, "w") as ol:
                    ol.write(tl.read())
        else:
            log.warning(f"{program_name} performing post-clang-format stdout post-processing")
            norm_str_file = io.StringIO(cf_res.stdout)
            logos_str_file = io.StringIO()
            log.info(f"{program_name} is re-Logos'ing the clang-format output")
            norm_to_logos(norm_str_file, logos_str_file)
            logos_str_file.seek(0, io.SEEK_SET)
            log.info(f"{program_name} is displaying the re-Logos'ed clang-format output")
            print(logos_str_file.read(), end="")
    return 0


class LogosHelpFormatter(argparse.HelpFormatter):
    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 24,
        width: int | None = None,
    ) -> None:
        super().__init__(prog, indent_increment, max_help_position, width)

    def format_help(self) -> str:
        logos_format_pat = re.compile(r"(?<!\.)clang-format")
        clang_help_str = subprocess.run(
            [clang_format_path, "-h"], capture_output=True, text=True
        ).stdout
        logos_help_lines = clang_help_str.splitlines()
        logos_help_lines[0] = f"OVERVIEW: {self._prog}: A tool to format Logos code."
        for i, line in enumerate(logos_help_lines):
            match: Optional[re.Match] = re.match(
                "^(?P<pre_space>\s+)--verbose(?P<post_space>\s+)-", line
            )
            if match is not None:
                num_chars_before_dash = len(match.group(0)) - 1
                logos_verbose_line = match.group("pre_space") + "--verbose-logos"
                num_spaces_after_verbose_logos = num_chars_before_dash - len(logos_verbose_line)
                logos_verbose_line = (
                    logos_verbose_line
                    + " " * num_spaces_after_verbose_logos
                    + f"- If set, shows verbose operation of {self._prog}."
                )
                logos_help_lines.insert(i + 1, logos_verbose_line)
                logos_save_temps_line = match.group("pre_space") + "--save-logos-temps"
                num_spaces_after_logos_save_temps = num_chars_before_dash - len(
                    logos_save_temps_line
                )
                logos_verbose_line = (
                    logos_save_temps_line
                    + " " * num_spaces_after_logos_save_temps
                    + f"- If set, don't delete temporary {self._prog} files."
                )
                logos_help_lines.insert(i + 2, logos_verbose_line)
        logos_help_str = "\n".join(logos_help_lines)
        logos_help_str = re.sub(logos_format_pat, self._prog, logos_help_str)
        logos_help_str = logos_help_str.replace(
            "Objective-C: .m .mm", "Objective-C: .m .mm .x .xi .xm .xmi"
        )
        return logos_help_str + "\n"


def get_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=program_name, formatter_class=LogosHelpFormatter)
    parser.add_argument(
        "--verbose-logos", action="store_true", help=f"Verbose output from {program_name}."
    )
    parser.add_argument(
        "--save-logos-temps",
        action="store_true",
        help=f"Don't delete temporary {program_name} files.",
    )
    parser.add_argument(
        "-i", action="store_true", dest="in_place", help="Inplace edit <file>s, if specified."
    )
    parser.add_argument(
        "--version", action="store_true", help="Display the version of this program"
    )
    return parser


def main() -> int:
    try:
        arg_parser = get_arg_parser()
        logos_format_args, clang_format_args = arg_parser.parse_known_intermixed_args()
        return real_main(logos_format_args, clang_format_args)
    except Exception as e:
        log.exception(f"Received an unexpected exception when running {program_name}")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
