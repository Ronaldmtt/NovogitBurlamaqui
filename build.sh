#!/bin/bash
set -e

echo "=========================================="
echo "   OPTIMIZED BUILD FOR REPLIT DEPLOY"
echo "   Single-layer approach to minimize size"
echo "=========================================="

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_BROWSERS_PATH=0

echo ""
echo "🚀 Installing dependencies and cleaning in SINGLE LAYER..."
echo "   (This prevents Docker layer bloat)"

pip install --no-cache-dir -r requirements.txt && \
    rm -rf ~/.cache/ms-playwright /home/runner/.cache/ms-playwright /root/.cache/ms-playwright 2>/dev/null || true && \
    rm -rf uploads/*.pdf attached_assets/ logs/*.log rpa_screenshots/*.png rpa_artifacts/* 2>/dev/null || true && \
    rm -rf *.backup.db bundle.txt 2>/dev/null || true && \
    pip cache purge 2>/dev/null || true && \
    rm -rf ~/.cache/pip /home/runner/.cache/pip /root/.cache/pip 2>/dev/null || true && \
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    find . -type f -name "*.pyc" -delete 2>/dev/null || true && \
    find . -type f -name "*.pyo" -delete 2>/dev/null || true && \
    rm -rf /tmp/* 2>/dev/null || true

echo ""
echo "✅ Verifying Playwright driver..."

PLAYWRIGHT_DRIVER=$(python3 -c "import playwright; import os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver', 'node'))" 2>/dev/null || echo "")
if [ -n "$PLAYWRIGHT_DRIVER" ] && [ -f "$PLAYWRIGHT_DRIVER" ]; then
    DRIVER_SIZE=$(du -h "$PLAYWRIGHT_DRIVER" 2>/dev/null | cut -f1)
    echo "   ✅ Playwright driver: $PLAYWRIGHT_DRIVER ($DRIVER_SIZE)"
else
    echo "   ❌ CRITICAL: Playwright driver not found!"
    echo "   Reinstalling playwright..."
    pip install --force-reinstall --no-cache-dir playwright && \
        rm -rf ~/.cache/ms-playwright /home/runner/.cache/ms-playwright 2>/dev/null || true
fi

CHROMIUM_PATH=$(which chromium 2>/dev/null || echo "")
if [ -n "$CHROMIUM_PATH" ]; then
    echo "   ✅ System Chromium: $CHROMIUM_PATH"
else
    echo "   ⚠️  System Chromium not in PATH (will check Nix at runtime)"
fi

echo ""
echo "📊 Final image size check..."
PYTHONLIBS_SIZE=$(du -sh /home/runner/workspace/.pythonlibs 2>/dev/null | cut -f1 || echo "N/A")
echo "   Python packages: $PYTHONLIBS_SIZE"

echo ""
echo "=========================================="
echo "✅ BUILD COMPLETE"
echo "=========================================="
echo "   - Single-layer install+cleanup (no bloat)"
echo "   - Playwright browsers: NEVER DOWNLOADED"
echo "   - Playwright driver: PRESERVED"
echo "   - RPA uses system Chromium via Nix"
echo "=========================================="
