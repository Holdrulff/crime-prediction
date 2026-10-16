# Previsão de registros de roubo em São Paulo

O projeto prepara dados públicos da SSP-SP para estudar contagens de registros de roubo por bairro e dia. A base tratada mantém os bairros informados pela SSP e todas as linhas selecionadas. A revisão de possíveis duplicações e a integração com o IBGE continuam sendo etapas posteriores.

## Artigo e análise exploratória

O [artigo em LaTeX](Template_SBC/template-latex/sbc-template.tex), com o estilo SBC e os três gráficos na mesma pasta, pode ser importado no Overleaf. A [versão compilada da análise exploratória](docs/relatorio_analise_exploratoria.pdf) acompanha o repositório. Os scripts `eda_preflight.py` e `eda_figures.py` documentam a auditoria e a geração dos gráficos; os resumos pequenos de referência ficam em `preflight/`. Para recriar os gráficos, o script usa ReportLab e fontes Arial do Windows, disponíveis no ambiente de elaboração; essas dependências são separadas do tratamento dos dados.

As bases Parquet em `data/ssp`, o ambiente Python e os arquivos temporários em `output/` são locais e ignorados pelo Git. O relatório de análise exploratória corresponde à etapa descrita no artigo; os resultados do tratamento são gerados separadamente em `data/ssp/relatorios/`.

## Executar

Requisitos: Python 3.12 e espaço para os arquivos originais e derivados. No PowerShell, a partir desta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe prepare_data.py
```

Os Excel grandes são lidos gradualmente. O processamento usa `lxml` para leitura e `pyarrow` para armazenamento Parquet; não exige banco de dados. É possível acompanhar a execução pelas mensagens de progresso. Os originais nunca são escritos pelo tratamento.

Em uma máquina com memória disponível, `prepare_data.py --workers 3` lê até três arquivos anuais em paralelo. O padrão é um arquivo por vez. Cada arquivo anual conferido fica salvo como ponto de retomada; o conjunto só deve ser aceito quando houver manifesto final e a verificação passar.

Uma nova execução confere as assinaturas dos originais, das regras, do código e das dependências. Arquivos anuais já tratados são reutilizados somente quando essas assinaturas e a assinatura do próprio Parquet conferem. Os resumos e painéis são reconstruídos. Para verificar se as saídas de uma execução completa continuam íntegras:

```powershell
.\.venv\Scripts\python.exe prepare_data.py --verify
```

## Organização

```text
data-sources/                    Excel originais já existentes no Git LFS
config/
  ssp_rules.json                 Regras e cobertura declarada dos arquivos
  bairro_aliases.csv             Equivalências de bairros revisadas e justificadas
data/ssp/                        Saídas reproduzíveis ignoradas pelo Git
  registros/
    SPDadosCriminais_ANO.parquet  Registros selecionados de cada arquivo anual
    SPDadosCriminais_ANO.json     Assinaturas, cabeçalhos, qualidade e cobertura
  analise/
    bairro_dia_ANO.parquet        Contagens e calendário de cada ano da ocorrência
  relatorios/
    resumo.md                    Resultado legível do tratamento
    dicionario.md                Tipos e significado dos campos
    qualidade.json               Reconciliação e indicadores estruturados
    totais_ano_ocorrencia.csv     Total municipal por ano do fato
    totais_diarios_municipio.csv  Contagens com e sem bairro por dia do fato
    bairros_observados.csv        Grafias originais e suas chaves usadas
    grupos_repetidos.csv         Grupos candidatos à revisão
    candidatos_duplicacao.parquet Linhas envolvidas nos grupos candidatos
  manifest.json                  Fontes, regras, versões e assinaturas das saídas
```

Para compartilhar a análise com o grupo, executar o tratamento a partir dos mesmos originais e código ou enviar uma cópia da pasta `data/ssp` completa. Conferir o manifesto com `--verify` antes de usar. Não é necessário colocar Parquet no Git nem contratar infraestrutura. Os arquivos em `preflight/` são a referência local da análise exploratória anterior: quando presentes, o tratamento exige igualdade das contagens selecionadas e por ano da ocorrência usando o conversor numérico anterior. O tratamento também aceita datas em texto ISO; caso recupere datas que antes não eram reconhecidas, registra a diferença em `data_ocorrencia_texto_recuperada`. A ausência da referência anterior é explicitada na metadata anual.

## Regras de tratamento

O [dicionário dos campos](docs/dicionario_dados.md) acompanha o código no Git. Cada execução também produz uma cópia junto aos resultados.

- O recorte usa o **município da ocorrência**, não o município de registro, e naturezas iniciadas por `ROUBO`.
- As colunas são localizadas por nomes e equivalências explícitas, incluindo as mudanças de cabeçalho de 2022 e a diferença de grafia de circunscrição. Campos obrigatórios ausentes ou ambíguos interrompem a execução.
- A data do fato orienta os totais. A data de registro é preservada, assim como os seriais originais do Excel. Ausência e falha de conversão recebem sinalizações diferentes. Datas anteriores ao intervalo principal e posteriores à cobertura permanecem nos registros e nos totais municipais.
- O status de data `valida` significa que o valor pôde ser convertido para uma data do calendário, sem comprovar sua plausibilidade como data de um crime. Anos anômalos permanecem visíveis nos totais por ano e fora dos painéis de 2022–2026; não são corrigidos por suposição.
- Bairro original e chave de agregação são campos distintos. A normalização remove acentos, uniformiza espaços e aplica maiúsculas. Não expande `JD`, `VL` ou outras abreviações automaticamente. Valores ausentes estão definidos nas regras.
- `bairro_aliases.csv` começa vazio. Para adicionar uma equivalência, preencher `origem,destino,justificativa` após conferência. O destino deve ser final: ciclos e cadeias de equivalências são rejeitados. Alterar esse arquivo provoca nova aplicação do tratamento.
- Coordenadas ausentes, inválidas, nulas, numericamente impossíveis ou fora da faixa preliminar de São Paulo são sinalizadas. Estar dentro dessa faixa retangular não comprova que o ponto esteja dentro do município. Falta de coordenadas não exclui um registro das contagens por bairro e dia.
- A chave candidata de BO combina delegacia, ano e número. O hash de linha considera todos os campos originais com cabeçalhos normalizados. Repetições são listadas, **não removidas**. Múltiplas naturezas no mesmo BO recebem uma sinalização específica e não comprovam duplicação.
- Cada registro preserva arquivo, aba, número real da linha e identificador de proveniência. Esse identificador não representa um crime único.

## Cobertura e zeros

A configuração declara 2022–2025 completos e janeiro–junho de 2026. Para confirmar a cobertura, o código exige as abas declaradas, todos os meses estatísticos esperados e ausência de registros estatísticos fora desse período. Isso verifica a cobertura declarada do arquivo, sem afirmar que todos os crimes tenham sido registrados ou que cada dia e bairro tenham cobertura completa.

Em cada intervalo, o universo de bairros é o conjunto de chaves observadas naquele intervalo. Quando a cobertura é confirmada, o painel contém todas as combinações dessas chaves com os dias do intervalo, preenchendo as ausentes com zero. A presença de cada bairro em todo o intervalo é uma hipótese de trabalho; as chaves não definem territórios oficiais. Se a cobertura não for confirmada, o painel contém somente pares observados e nenhuma linha de zero é inventada.

Zero significa **nenhum registro encontrado na base**, não ausência de crime. Os painéis são separados por ano da ocorrência, e 2026 recebe `periodo_parcial=True`. Registros sem data não entram nos painéis. Registros com data, mas sem bairro, aparecem nos totais municipais e na reconciliação. Registros fora dos intervalos declarados também permanecem nos resumos municipais.

## Antes de treinar modelos

As contagens históricas incorporam registros tardios encontrados nas planilhas posteriores. Portanto, o painel não reproduz o que era conhecido em cada data. Na modelagem, reconstruir a disponibilidade de informação pela data de registro: para prever no início de um dia, usar apenas registros disponíveis antes desse dia. Registros sem data de registro utilizável e datas inconsistentes exigem revisão. Não usar a hora ou o local de um fato futuro como variável preditora.

Ainda é necessário revisar os candidatos a duplicação, as equivalências entre bairros e a estabilidade da unidade bairro–dia. Os painéis contam **registros preservados**; não os chamar de ocorrências únicas. Não há modelos treinados, geocodificação, limites oficiais de bairros ou indicadores socioeconômicos nesta etapa.

## Conferências automatizadas

Os testes usam planilhas pequenas com mudanças de cabeçalhos, datas do Excel, células de texto, saltos de número de linha, dados ausentes, coordenadas inválidas e BOs repetidos com múltiplas naturezas. Verificam preservação dos originais, reconciliação, ausência de pares bairro–dia repetidos, contagens não negativas, ausência de preenchimento zero sem cobertura e repetição determinística das saídas. A execução real também compara os totais com a análise anterior e registra as assinaturas de todos os arquivos gerados.
