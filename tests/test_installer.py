"""Installer behaviour that can be checked without root or systemd.

The script supports a library mode (NFP_INSTALLER_LIB=1) that stops before any
root-only work, so the message catalog and the validators can be exercised in
CI on any machine that has bash.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install.sh"
BASH = shutil.which("bash")


def clean_env():
    # Never inherit NFP_INSTALLER_LIB from the caller: the script would stop at
    # its library guard and every check below would silently see empty output.
    environment = dict(os.environ)
    environment.pop("NFP_INSTALLER_LIB", None)
    return environment


def run(args, env=None):
    if env is None:
        env = clean_env()
    return subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        # bash prints UTF-8; without this the reader thread dies on a GBK host
        # and stdout silently becomes None.
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )


def run_library_with_stdin(stdin_text, code):
    environment = clean_env()
    environment["NFP_INSTALLER_LIB"] = "1"
    # Feed stdin as bytes: text mode would translate \n to \r\n on Windows and
    # bash's read would hand the script "2\r" instead of "2".
    result = subprocess.run(
        [BASH, "-c", "source ./install.sh\n" + code],
        cwd=str(ROOT),
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        timeout=120,
        env=environment,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


def run_library(code):
    environment = clean_env()
    environment["NFP_INSTALLER_LIB"] = "1"
    return run([BASH, "-c", "source ./install.sh\n" + code], env=environment)


@unittest.skipUnless(BASH, "bash is required")
class InstallerTests(unittest.TestCase):
    def test_script_syntax_is_valid(self):
        result = run([BASH, "-n", str(SCRIPT)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_is_available_in_both_languages(self):
        chinese = run([BASH, str(SCRIPT), "--help"])
        english = run([BASH, str(SCRIPT), "--lang", "en", "--help"])
        self.assertEqual(chinese.returncode, 0, chinese.stderr)
        self.assertEqual(english.returncode, 0, english.stderr)
        self.assertIn("访问方式", chinese.stdout)
        self.assertIn("卸载", chinese.stdout)
        self.assertIn("Access mode", english.stdout)
        self.assertIn("--uninstall", english.stdout)
        self.assertNotIn("Access mode", chinese.stdout)

    def test_access_modes_and_uninstall_flags_are_documented(self):
        english = run([BASH, str(SCRIPT), "--lang", "en", "--help"]).stdout
        for flag in ("--access https", "--access http", "--port", "--domain", "--email",
                     "--uninstall", "--purge", "--keep-rules", "--yes", "--lang"):
            self.assertIn(flag, english)

    def test_library_mode_switches_language(self):
        result = run_library(
            "LANG_CODE=zh; t access_prompt; LANG_CODE=en; t access_prompt; "
            "LANG_CODE=en; t port_prompt 8080"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertIn("访问方式", lines[0])
        self.assertIn("Access mode", lines[1])
        self.assertIn("8080", lines[2])

    def test_validators(self):
        result = run_library(
            'for p in 0 65536 8108; do if valid_port "$p"; then echo "port:$p:ok"; else echo "port:$p:no"; fi; done; '
            'for u in admin ab "a b"; do if valid_username "$u"; then echo "user:$u:ok"; else echo "user:$u:no"; fi; done; '
            'for d in panel.example.com nope; do if valid_domain "$d"; then echo "domain:$d:ok"; else echo "domain:$d:no"; fi; done; '
            'for w in short 123456789012; do if valid_password "$w"; then echo "pw:$w:ok"; else echo "pw:$w:no"; fi; done'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        for expected in (
            "port:0:no",
            "port:65536:no",
            "port:8108:ok",
            "user:admin:ok",
            "user:ab:no",
            "domain:panel.example.com:ok",
            "domain:nope:no",
            "pw:short:no",
            "pw:123456789012:ok",
        ):
            self.assertIn(expected, out)


    def test_switching_to_https_closes_the_previous_http_port(self):
        # firewall_close_port must be reachable outside the uninstall path:
        # the definition plus at least two call sites (switch + uninstall).
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("firewall_close_port"), 3)
        self.assertIn('if [[ "$previous_mode" == "http" && -n "$previous_port" ]]', text)

    def test_language_question_selects_the_following_language(self):
        english = run_library_with_stdin("2\n", "ask_language; echo \"picked=$LANG_CODE\"; t access_prompt")
        chinese = run_library_with_stdin("1\n", "ask_language; echo \"picked=$LANG_CODE\"; t access_prompt")
        self.assertIn("picked=en", english.stdout)
        self.assertIn("Access mode", english.stdout)
        self.assertIn("picked=zh", chinese.stdout)
        self.assertIn("访问方式", chinese.stdout)

    def test_language_flag_skips_the_question(self):
        result = run_library_with_stdin("2\n", "LANG_EXPLICIT=1; LANG_CODE=zh; ask_language; echo \"kept=$LANG_CODE\"")
        self.assertIn("kept=zh", result.stdout)
        self.assertNotIn("Choose language", result.stdout)


if __name__ == "__main__":
    unittest.main()
