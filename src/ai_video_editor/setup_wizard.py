"""First-run setup wizard — checks prerequisites and guides API key configuration.

Runs automatically on first `vx` launch (no .vx.json or no API keys).
Can be re-run via `vx setup`.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .infra.atomic_write import atomic_write_text

import questionary
from questionary import Style

from .i18n import t, get_available_locales, set_locale, get_locale

VX_STYLE = Style(
    [
        ("qmark", "fg:#2ecc71 bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", "fg:#2ecc71"),
        ("pointer", "fg:#2ecc71 bold"),
        ("highlighted", "fg:#2ecc71 bold"),
        ("selected", "fg:#2ecc71"),
        ("instruction", "fg:#666666"),
        ("text", "fg:#aaaaaa"),
    ]
)

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def needs_setup() -> bool:
    """Check whether first-run setup should be triggered."""
    vx_config = Path(".vx.json")
    if vx_config.exists():
        try:
            data = json.loads(vx_config.read_text())
            if data.get("setup_complete"):
                return False
        except Exception:
            pass

    # No config at all, or setup not marked complete
    return True


def run_setup_wizard() -> bool:
    """Run the interactive first-run setup wizard.

    Returns True if setup completed successfully, False if user cancelled.
    """
    print(f"\n{_BOLD}  {t('app.title')}{_RESET}")
    print(f"{_DIM}  {t('app.subtitle')}{_RESET}")
    print(f"\n  {_BOLD}{t('setup.title')}{_RESET}\n")

    # --- Step 1: Language selection ---
    locales = get_available_locales()
    if len(locales) > 1:
        locale_choices = [
            questionary.Choice(f"{loc['name']}", value=loc["code"]) for loc in locales
        ]
        selected_locale = questionary.select(
            t("setup.language_prompt"),
            choices=locale_choices,
            default=get_locale(),
            style=VX_STYLE,
        ).ask()
        if selected_locale is None:
            return False
        if selected_locale != get_locale():
            set_locale(selected_locale)

    # --- Step 2: Check prerequisites ---
    checks = _check_prerequisites()
    _print_checks(checks)
    print()

    if not checks["python_ok"]:
        print(f"  {_RED}{t('setup.python_missing')}{_RESET}")
        print(f"  https://www.python.org/downloads/\n")
        # Python is obviously present if we're running, but version might be old
        # Continue anyway since we're already executing

    if not checks["ffmpeg_ok"]:
        print(f"  {_RED}{t('setup.ffmpeg_missing')}{_RESET}")
        install_cmd = _get_ffmpeg_install_command()
        if install_cmd:
            print(f"  {t('setup.ffmpeg_install_hint', command=install_cmd)}")
        else:
            print(f"  {t('setup.ffmpeg_install_hint', command=t('setup.ffmpeg_install_generic'))}")
        print()

        cont = questionary.confirm(
            t("setup.continue_anyway"),
            default=True,
            style=VX_STYLE,
        ).ask()
        if not cont:
            return False

    # --- Step 3: API key configuration ---
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if not gemini_key and not anthropic_key:
        print(f"  {t('setup.provider_intro')}\n")

        provider = questionary.select(
            t("setup.choose_provider"),
            choices=[
                questionary.Choice(t("setup.provider_gemini"), value="gemini"),
                questionary.Choice(t("setup.provider_claude"), value="claude"),
                questionary.Choice(t("setup.provider_both"), value="both"),
            ],
            style=VX_STYLE,
        ).ask()
        if provider is None:
            return False

        env_lines = _read_env_file()

        if provider in ("gemini", "both"):
            key = questionary.password(
                t("setup.gemini_key_prompt"),
                instruction=t("setup.gemini_key_hint"),
                style=VX_STYLE,
            ).ask()
            if key and key.strip():
                env_lines = _set_env_value(env_lines, "GEMINI_API_KEY", key.strip())
                os.environ["GEMINI_API_KEY"] = key.strip()
                print(f"  {_GREEN}✓{_RESET} {t('setup.api_key_verified', provider='Gemini')}")

        if provider in ("claude", "both"):
            key = questionary.password(
                t("setup.anthropic_key_prompt"),
                instruction=t("setup.anthropic_key_hint"),
                style=VX_STYLE,
            ).ask()
            if key and key.strip():
                env_lines = _set_env_value(env_lines, "ANTHROPIC_API_KEY", key.strip())
                os.environ["ANTHROPIC_API_KEY"] = key.strip()
                print(f"  {_GREEN}✓{_RESET} {t('setup.api_key_verified', provider='Claude')}")

        _write_env_file(env_lines)
        print(f"  {_DIM}{t('setup.api_key_saved')}{_RESET}")

        # Set default provider in workspace config
        default_provider = "gemini" if provider in ("gemini", "both") else "claude"
    else:
        default_provider = "gemini" if gemini_key else "claude"
        if gemini_key:
            print(f"  {_GREEN}✓{_RESET} {t('setup.api_key_found', provider='Gemini')}")
        if anthropic_key:
            print(f"  {_GREEN}✓{_RESET} {t('setup.api_key_found', provider='Claude')}")

    # --- Step 4: Save config ---
    vx_config = Path(".vx.json")
    config = {}
    if vx_config.exists():
        try:
            config = json.loads(vx_config.read_text())
        except Exception:
            pass

    config["setup_complete"] = True
    config.setdefault("provider", default_provider)
    config.setdefault("style", "vlog")

    # Save locale if non-English
    current_locale = get_locale()
    if current_locale != "en":
        config["locale"] = current_locale

    atomic_write_text(vx_config, json.dumps(config, indent=2) + "\n")

    print(f"\n  {_GREEN}✓{_RESET} {t('setup.setup_complete')}\n")
    return True


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------


def _check_prerequisites() -> dict:
    """Check for Python version, ffmpeg, and API keys."""
    result = {
        "python_ok": False,
        "python_version": "",
        "ffmpeg_ok": False,
        "ffmpeg_version": "",
        "gemini_key": False,
        "anthropic_key": False,
    }

    # Python version
    vi = sys.version_info
    result["python_version"] = f"{vi.major}.{vi.minor}.{vi.micro}"
    result["python_ok"] = vi >= (3, 11)

    # ffmpeg
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            out = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Parse version from first line: "ffmpeg version 7.1 ..."
            first_line = out.stdout.split("\n")[0] if out.stdout else ""
            parts = first_line.split()
            if len(parts) >= 3:
                result["ffmpeg_version"] = parts[2].split("-")[0]
            else:
                result["ffmpeg_version"] = "found"
            result["ffmpeg_ok"] = True
        except Exception:
            pass

    # API keys
    result["gemini_key"] = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    result["anthropic_key"] = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    return result


def _print_checks(checks: dict):
    """Print prerequisite check results."""
    ok = f"{_GREEN}✓{_RESET}"
    fail = f"{_RED}✗{_RESET}"

    # Python
    if checks["python_ok"]:
        print(f"  {ok} {t('setup.python_found', version=checks['python_version'])}")
    else:
        print(f"  {fail} {t('setup.python_found', version=checks['python_version'])} — {t('setup.python_missing')}")

    # ffmpeg
    if checks["ffmpeg_ok"]:
        print(f"  {ok} {t('setup.ffmpeg_found', version=checks['ffmpeg_version'])}")
    else:
        print(f"  {fail} {t('setup.ffmpeg_missing')}")

    # API keys
    if checks["gemini_key"]:
        print(f"  {ok} {t('setup.api_key_found', provider='Gemini')}")
    elif checks["anthropic_key"]:
        print(f"  {ok} {t('setup.api_key_found', provider='Claude')}")
    else:
        print(f"  {fail} {t('setup.no_api_key')}")


def _get_ffmpeg_install_command() -> str | None:
    """Suggest an ffmpeg install command based on the platform."""
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return t("setup.ffmpeg_install_brew")
    elif sys.platform == "linux":
        if shutil.which("apt"):
            return t("setup.ffmpeg_install_apt")
        if shutil.which("dnf"):
            return "sudo dnf install ffmpeg"
        if shutil.which("pacman"):
            return "sudo pacman -S ffmpeg"
    elif sys.platform == "win32":
        if shutil.which("choco"):
            return "choco install ffmpeg"
        if shutil.which("winget"):
            return "winget install ffmpeg"
    return None


# ---------------------------------------------------------------------------
# .env file helpers
# ---------------------------------------------------------------------------


def _read_env_file() -> list[str]:
    """Read existing .env file lines, or create from .env.example."""
    env_path = Path(".env")
    example_path = Path(".env.example")

    if env_path.exists():
        return env_path.read_text().splitlines()

    if example_path.exists():
        return example_path.read_text().splitlines()

    # Create minimal template
    return [
        "# Gemini API key (https://aistudio.google.com/apikey)",
        "GEMINI_API_KEY=",
        "",
        "# Anthropic API key (https://console.anthropic.com/)",
        "ANTHROPIC_API_KEY=",
    ]


def _set_env_value(lines: list[str], key: str, value: str) -> list[str]:
    """Set a key=value in the env lines, updating in place or appending."""
    updated = False
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            result.append(f"{key}={value}")
            updated = True
        else:
            result.append(line)
    if not updated:
        result.append(f"{key}={value}")
    return result


def _write_env_file(lines: list[str]):
    """Write lines back to .env file."""
    Path(".env").write_text("\n".join(lines) + "\n")
