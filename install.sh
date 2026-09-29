#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HOME/.local/share/claude-usage-indicator"
SERVICE_DIR="$HOME/.config/systemd/user"
ICON_DIR="$STATE_DIR/icons"
OPENAI_ICON="$ICON_DIR/openai-blossom-trimmed.svg"
OPENAI_LOGO_URL="https://cdn.openai.com/brand/openai-logos.zip"
OPENAI_LOGO_MEMBER="OpenAI-logos/SVGs/OAI_OpenAI-Blossom_White.svg"

# The OpenAI logo is OpenAI's trademark, so it isn't shipped in this repo.
# Ask before fetching it from OpenAI's brand page. Set INSTALL_OPENAI_LOGO=yes
# or =no to skip the prompt.
install_openai_logo() {
    # Untrimmed logo from earlier installs.
    rm -f "$ICON_DIR/openai-blossom.svg"

    if [ -f "$OPENAI_ICON" ]; then
        echo "OpenAI logo already installed."
        return
    fi

    local answer="${INSTALL_OPENAI_LOGO:-}"
    if [ -z "$answer" ]; then
        if [ -t 0 ]; then
            echo
            echo "The Codex indicator can show the OpenAI logo instead of the word \"Codex\"."
            echo "It is downloaded from $OPENAI_LOGO_URL (~70 KB)."
            echo "By downloading it you agree to OpenAI's brand guidelines and Marks"
            echo "usage terms: https://openai.com/brand/"
            read -r -p "Download the OpenAI logo? [y/N] " answer
        else
            answer=no
        fi
    fi

    case "$answer" in
        [yY] | [yY][eE][sS]) ;;
        *)
            echo "Skipping the OpenAI logo; the Codex indicator will show \"Codex\" instead."
            return
            ;;
    esac

    local tmp
    tmp="$(mktemp -d)"
    if curl -fsSL -o "$tmp/openai-logos.zip" "$OPENAI_LOGO_URL" &&
        mkdir -p "$ICON_DIR" &&
        python3 - "$tmp/openai-logos.zip" "$OPENAI_LOGO_MEMBER" "$OPENAI_ICON" <<'EOF'
import re, sys, zipfile
src, member, dest = sys.argv[1:]
with zipfile.ZipFile(src) as z:
    svg = z.read(member).decode()

# The logo's canvas includes a lot of clear space, which makes the mark tiny
# at panel size. Shrink the viewBox to the mark's bounds. Only done when all
# path commands are absolute, so the numbers really are coordinates; control
# points bound the curves, so this never clips the mark.
paths = " ".join(re.findall(r'\sd="([^"]+)"', svg))
if paths and not re.search(r"[a-z]", paths):
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", paths)]
    margin = 8
    x0, x1 = min(nums[0::2]) - margin, max(nums[0::2]) + margin
    y0, y1 = min(nums[1::2]) - margin, max(nums[1::2]) + margin
    size = max(x1 - x0, y1 - y0)
    svg = re.sub(r'<svg\b[^>]*?>', lambda m: re.sub(
        r'\s(width|height|viewBox)="[^"]*"', "", m.group(0)
    ).replace("<svg", f'<svg width="{size:g}" height="{size:g}" '
              f'viewBox="{x0:g} {y0:g} {size:g} {size:g}"', 1), svg, count=1)

with open(dest, "w") as f:
    f.write(svg)
EOF
    then
        echo "Installed the OpenAI logo to $OPENAI_ICON"
    else
        rm -f "$OPENAI_ICON"
        echo "Couldn't download the OpenAI logo; the Codex indicator will show \"Codex\" instead."
    fi
    rm -rf "$tmp"
}

echo "Installing gir1.2-ayatanaappindicator3-0.1 (requires sudo)..."
sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1

mkdir -p "$STATE_DIR" "$SERVICE_DIR"
cp "$SCRIPT_DIR/indicator.py" "$STATE_DIR/indicator.py"
cp "$SCRIPT_DIR/claude-usage-indicator.service" "$SERVICE_DIR/claude-usage-indicator.service"

install_openai_logo

systemctl --user daemon-reload
systemctl --user enable claude-usage-indicator.service
# restart (not just start) so re-running the installer picks up changes.
systemctl --user restart claude-usage-indicator.service

echo "Installed. Check status with:"
echo "  systemctl --user status claude-usage-indicator.service"
echo "  journalctl --user -u claude-usage-indicator.service -f"
