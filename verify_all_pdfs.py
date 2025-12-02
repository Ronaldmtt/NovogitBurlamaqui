#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de Verificação Completa de Extração de PDFs
Analisa TODOS os PDFs em uploads/ e exibe todos os campos extraídos
para identificar possíveis erros de extração e efeitos cascata.
"""
import os
import sys
from pathlib import Path
from PyPDF2 import PdfReader
from extractors.pipeline import run_extraction_from_text
from extractors.postprocess import full_postprocess
import json

def extract_text_from_pdf(pdf_path):
    """Extrai texto completo do PDF"""
    try:
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        return f"ERRO ao ler PDF: {e}"

def analyze_pdf(pdf_path):
    """Analisa um PDF e retorna dados extraídos"""
    filename = os.path.basename(pdf_path)
    print(f"\n{'='*80}")
    print(f"📄 PDF: {filename}")
    print(f"{'='*80}")
    
    # Extrair texto
    text = extract_text_from_pdf(pdf_path)
    if text.startswith("ERRO"):
        print(f"❌ {text}")
        return None
    
    # Executar pipeline de extração
    try:
        raw_data = run_extraction_from_text(text, filename=filename)
        data = full_postprocess(raw_data, text)
    except Exception as e:
        print(f"❌ ERRO na extração: {e}")
        return None
    
    # ===== CAMPOS CRÍTICOS COM EFEITO CASCATA =====
    print("\n🔴 CAMPOS CRÍTICOS (Efeito Cascata):")
    print(f"  • Origem:           {data.get('origem', 'NÃO DETECTADO')}")
    print(f"  • Órgão:            {data.get('orgao', 'NÃO DETECTADO')}")
    print(f"  • Instância:        {data.get('instancia', 'NÃO DETECTADO')}")
    print(f"  • Foro:             {data.get('foro', 'NÃO DETECTADO')}")
    print(f"  • Comarca:          {data.get('comarca', 'NÃO DETECTADO')}")
    print(f"  • Estado (UF):      {data.get('estado', 'NÃO DETECTADO')}")
    
    # ===== PARTES E TIPO DE PESSOA =====
    print("\n👥 PARTES:")
    print(f"  • Reclamante:       {data.get('reclamante', 'NÃO DETECTADO')}")
    print(f"  • Tipo Reclamante:  {data.get('reclamante_tipo', 'NÃO DETECTADO')}")
    print(f"  • Reclamado:        {data.get('reclamado', 'NÃO DETECTADO')}")
    print(f"  • Tipo Reclamado:   {data.get('reclamado_tipo', 'NÃO DETECTADO')}")
    
    # ===== CLIENTE E CÉLULA =====
    print("\n🏢 CLIENTE:")
    print(f"  • Cliente:          {data.get('cliente', 'NÃO DETECTADO')}")
    print(f"  • Parte Interessada: {data.get('parte_interessada', 'NÃO DETECTADO')}")
    print(f"  • Célula:           {data.get('celula', 'NÃO DETECTADO')}")
    print(f"  • Outra Reclamada:  {data.get('outra_reclamada_cliente', 'NÃO DETECTADO')}")
    
    # ===== DADOS PROCESSUAIS =====
    print("\n📋 DADOS PROCESSUAIS:")
    print(f"  • CNJ:              {data.get('cnj', 'NÃO DETECTADO')}")
    print(f"  • Processo Antigo:  {data.get('processo_antigo', 'NÃO DETECTADO')}")
    print(f"  • Rito:             {data.get('rito', 'NÃO DETECTADO')}")
    print(f"  • Valor da Causa:   {data.get('valor_causa', 'NÃO DETECTADO')}")
    
    # ===== DATAS =====
    print("\n📅 DATAS:")
    print(f"  • Distribuição:     {data.get('data_distribuicao', 'NÃO DETECTADO')}")
    print(f"  • Audiência Inicial: {data.get('audiencia_inicial', 'NÃO DETECTADO')}")
    print(f"  • Cadastrar 1ª Aud: {data.get('cadastrar_primeira_audiencia', 'NÃO DETECTADO')}")
    
    # ===== CLASSIFICAÇÃO DO DOCUMENTO =====
    print("\n📄 CLASSIFICAÇÃO:")
    print(f"  • Tipo Documento:   {data.get('tipo_documento', 'NÃO DETECTADO')}")
    print(f"  • Confiança:        {data.get('confianca', 'NÃO DETECTADO')}")
    
    # ===== ADVOGADOS =====
    advogados = data.get('advogados', [])
    if advogados:
        print(f"\n⚖️ ADVOGADOS: {', '.join(advogados)}")
    
    # ===== VERIFICAÇÃO DE EFEITO CASCATA =====
    print("\n🔍 VERIFICAÇÃO DE EFEITO CASCATA:")
    
    # Verificar se origem = TST quando deveria ser TRT
    if data.get('origem') == 'TST':
        if 'Vara' in text or 'Petição Inicial' in text or 'Rito' in text:
            print("  ⚠️  ALERTA: Origem = TST mas PDF contém sinais de 1ª instância (Vara/Petição/Rito)")
    
    # Verificar se instância = 2ª quando deveria ser 1ª
    if data.get('instancia') == '2ª Instância':
        if 'Distribuído em' in text and 'Vara' in text:
            print("  ⚠️  ALERTA: Instância = 2ª mas PDF tem 'Distribuído em' + 'Vara' (sinais de 1ª inst)")
    
    # Verificar se Foro está vazio
    if not data.get('foro'):
        print("  ⚠️  ALERTA: Campo Foro VAZIO (pode causar erro no eLaw)")
    
    # Verificar se tipo de pessoa está incorreto (PJ para pessoa com nome de pessoa física)
    reclamante_tipo = data.get('reclamante_tipo', '')
    reclamante_nome = data.get('reclamante', '')
    if reclamante_tipo == 'PESSOA JURIDICA':
        # Verificar se tem nome típico de PF (ex: MARIA, JOSÉ, JOÃO, ANTONIO)
        nomes_pf = ['MARIA', 'JOSE', 'JOAO', 'ANTONIO', 'ANTONIA', 'FRANCISCO']
        if any(nome in reclamante_nome.upper() for nome in nomes_pf):
            print(f"  ⚠️  ALERTA: Reclamante '{reclamante_nome}' classificado como PJ mas parece ser PF")
    
    return data

def main():
    """Processa todos os PDFs em uploads/"""
    uploads_dir = Path("uploads")
    
    if not uploads_dir.exists():
        print("❌ Pasta uploads/ não encontrada!")
        return
    
    pdf_files = sorted(uploads_dir.glob("*.pdf"))
    
    if not pdf_files:
        print("❌ Nenhum PDF encontrado em uploads/")
        return
    
    print(f"\n🔎 Encontrados {len(pdf_files)} PDFs para análise")
    print(f"{'='*80}\n")
    
    resultados = []
    
    for pdf_path in pdf_files:
        data = analyze_pdf(str(pdf_path))
        if data:
            resultados.append({
                'arquivo': pdf_path.name,
                'dados': data
            })
    
    # Salvar resultados em JSON
    output_file = "verificacao_pdfs_resultado.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*80}")
    print(f"✅ Análise completa! Resultados salvos em: {output_file}")
    print(f"{'='*80}\n")
    
    # Estatísticas gerais
    print("\n📊 ESTATÍSTICAS GERAIS:")
    print(f"  • Total de PDFs analisados: {len(resultados)}")
    
    origens = {}
    instancias = {}
    clientes = {}
    
    for r in resultados:
        origem = r['dados'].get('origem', 'NÃO DETECTADO')
        origens[origem] = origens.get(origem, 0) + 1
        
        inst = r['dados'].get('instancia', 'NÃO DETECTADO')
        instancias[inst] = instancias.get(inst, 0) + 1
        
        cliente = r['dados'].get('cliente', 'NÃO DETECTADO')
        clientes[cliente] = clientes.get(cliente, 0) + 1
    
    print("\n  📍 Distribuição por ORIGEM:")
    for origem, count in sorted(origens.items()):
        print(f"     - {origem}: {count} PDFs")
    
    print("\n  📍 Distribuição por INSTÂNCIA:")
    for inst, count in sorted(instancias.items()):
        print(f"     - {inst}: {count} PDFs")
    
    print("\n  📍 Distribuição por CLIENTE:")
    for cliente, count in sorted(clientes.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"     - {cliente}: {count} PDFs")

if __name__ == "__main__":
    main()
