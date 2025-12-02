#!/bin/bash
# Script de limpeza agressiva para reduzir tamanho do deploy

echo "=========================================="
echo "   LIMPEZA AGRESSIVA PARA DEPLOY"
echo "=========================================="

echo ""
echo "🗑️  1. Removendo PDFs de teste e uploads..."
rm -rf uploads/*.pdf 2>/dev/null || true
rm -rf attached_assets/ 2>/dev/null || true

echo "🗑️  2. Limpando caches Python..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true

echo "🗑️  3. Limpando logs e screenshots de RPA..."
rm -rf logs/*.log 2>/dev/null || true
rm -rf rpa_screenshots/*.png 2>/dev/null || true
rm -rf rpa_artifacts/* 2>/dev/null || true

echo "🗑️  4. Limpando arquivos temporários..."
rm -rf /tmp/* 2>/dev/null || true
rm -rf *.backup.db 2>/dev/null || true
rm -rf bundle.txt 2>/dev/null || true

echo "🗑️  5. Limpando pip cache..."
pip cache purge 2>/dev/null || true
rm -rf ~/.cache/pip /home/runner/.cache/pip /root/.cache/pip 2>/dev/null || true

echo "🗑️  6. REMOVENDO BROWSERS DO PLAYWRIGHT (salva ~6GB)..."
echo "   NOTA: Mantendo o driver do Playwright (necessário para execução)"
rm -rf ~/.cache/ms-playwright 2>/dev/null || true
rm -rf /home/runner/.cache/ms-playwright 2>/dev/null || true
rm -rf /root/.cache/ms-playwright 2>/dev/null || true

find /home/runner/.cache -type d -name "chromium*" -exec rm -rf {} + 2>/dev/null || true
find /home/runner/.cache -type d -name "firefox*" -exec rm -rf {} + 2>/dev/null || true
find /home/runner/.cache -type d -name "webkit*" -exec rm -rf {} + 2>/dev/null || true

echo ""
echo "=========================================="
echo "✅ LIMPEZA CONCLUÍDA!"
echo "=========================================="
echo "   - Browsers Playwright: REMOVIDOS (~6GB)"
echo "   - PDFs de teste: REMOVIDOS (~150MB)"
echo "   - Caches: LIMPOS (~500MB)"
echo "=========================================="
