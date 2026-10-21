# Dados de roubos na cidade de São Paulo

Este repositório reúne os dados públicos originais da SSP-SP, os registros normalizados e exemplos para prever contagens de registros de roubo por bairro na semana seguinte. As entradas usam históricos de 28, 56 ou 84 dias. Há scripts de preparação e conferência; os modelos ainda não foram treinados.

O snapshot normalizado é de **16 de outubro de 2026** e usa exclusivamente as cinco planilhas locais de 2022 a 2026. O arquivo de 2026 contém janeiro a junho. O escopo atual usa somente SSP-SP; não inclui integração com o IBGE.

## Estrutura

```text
data-sources/                         Planilhas Excel originais preservadas
normalized-data/
  registros/                         Um Parquet por arquivo anual da SSP
  conjuntos/
    treino.parquet                   Datas de ocorrência de 2022 e 2023
    validacao.parquet                Datas de ocorrência de 2024
    teste.parquet                    Datas de ocorrência de 2025
    avaliacao_2026.parquet            Janeiro a junho de 2026
  auditoria/
    resumo.json                      Totais e reconciliação das exclusões dos painéis
    resumo_conjuntos.csv              Dimensões dos conjuntos para consulta
    registros_fora_paineis.csv        Identificadores e motivos de não inclusão
    revisao_boletins.csv              Revisão dos seis grupos de BOs repetidos
    cobertura_revisada.json           Auditoria das linhas vazias dos Excel
  metadados/
    dicionario.md                     Significado dos campos
    regras.json                      Regras de tratamento e cortes temporais
    bairro_aliases.csv               Equivalências de bairros revisadas atualmente vazio
    manifest.json                    Assinaturas SHA256 das fontes e das saídas
README.md
```

A preparação semanal acrescenta `normalized-data/modelagem/`, `scripts/`, `tests/` e `requirements.txt`. Os arquivos originais e os painéis diários acima foram preservados.

## Base semanal para modelagem

Cada exemplo representa uma previsão emitida no início da segunda-feira para a contagem da semana, de segunda a domingo. As três janelas têm os mesmos pares bairro–semana e começam após 84 dias de cobertura. Semanas que cruzam divisões ou o fim da cobertura não entram na avaliação. O bairro é um rótulo textual da SSP, sem limites geográficos oficiais ou relações de vizinhança modeladas.

Arquivos em `normalized-data/modelagem/`:

- `treino/`, `validacao/`, `teste/` e `avaliacao_2026/`: cada pasta contém `janela_28.parquet`, `janela_56.parquet` e `janela_84.parquet`.
- `contribuicoes_alvo.parquet`: contagens agrupadas por bairro, semana da ocorrência e data de registro, necessárias para reconstruir alvos disponíveis em uma data de treinamento.
- `bairros_desconhecidos.parquet`: contagens de bairros ainda não conhecidos no início da semana prevista, fora da avaliação principal.
- `relatorio.md`, `relatorio.json` e `conferencia_semanal.csv`: dimensões e reconciliação dos registros.
- `dicionario.md`: definição dos campos e limitações.
- `manifest.json`: hashes dos resultados, do script e do manifesto de entrada.

### Executar a preparação

Requer Python 3.10 ou superior. Os binários de entrada devem estar disponíveis pelo Git LFS.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/prepare_weekly_data.py build --output normalized-data/modelagem_reconstruida
python scripts/verify_weekly_data.py --output normalized-data/modelagem_reconstruida
```

O destino deve estar vazio para evitar sobrescritas e mistura entre execuções. Para produzir a pasta principal na primeira execução, basta omitir `--output`. Os hashes permitem comparar uma reconstrução com a versão publicada; usar a mesma versão de Python e PyArrow para comparação binária.

### Disponibilidade e alvos de treinamento

As entradas e o cadastro de bairros usam somente registros com `data_registro < inicio_semana`. As contagens históricas também exigem ocorrência dentro da janela e anterior à previsão. Bairros permanecem no cadastro após serem conhecidos; zeros anteriores à primeira observação significam ausência de registros disponíveis, sem comprovar existência territorial ou ausência de crimes.

`alvo_retrospectivo` é a contagem final **no snapshot disponível**, destinada à avaliação. **Não deve ser usado diretamente para ajustar um modelo em uma simulação histórica.** Para treinar em uma data, reconstruir os alvos com o comando abaixo:

```bash
python scripts/prepare_weekly_data.py targets --examples normalized-data/modelagem/treino/janela_28.parquet --as-of 2024-01-01 --output normalized-data/alvos_treinamento/treino_28_corte_2024-01-01.parquet
```

O comando remove `alvo_retrospectivo`, inclui `alvo_disponivel` e `data_corte_treinamento`, e exclui semanas ainda abertas na data de corte. O corte é exclusivo: registros feitos no próprio dia ficam de fora. A mesma operação funciona para as demais janelas e datas. Os alvos disponíveis podem estar incompletos por atraso; uma política de maturação e eventuais reajustes do modelo ainda precisam ser avaliados na etapa de treinamento. O arquivo de contribuições contém informação futura e deve servir apenas à reconstrução filtrada dos alvos e à auditoria, nunca como entrada direta do modelo.

A data de registro é uma aproximação da disponibilidade, não uma comprovação de quando cada boletim foi publicado. O snapshot não conserva todas as versões históricas dos boletins. Ocorrências próximas ao fim de junho de 2026 também podem estar incompletas por registros tardios ainda ausentes.

Na seleção dos modelos, usar 2024; reservar 2025 para o teste final. Não ajustar codificadores com bairros de períodos futuros. Antes de treinar, selecionar explicitamente as colunas preditoras: bairro, mês e atributos históricos; datas identificam as previsões, e alvos/colunas de auditoria não são preditores. As janelas não usam eventos futuros nem dados socioeconômicos.

Os arquivos ocultos `.gitattributes` e `.gitignore` são configurações técnicas do Git. Os Excel e os Parquet usam **Git LFS**. Instale o Git LFS antes de clonar o repositório; em uma cópia já existente, execute `git lfs pull`. Sem isso, podem aparecer apenas arquivos de ponteiro, em vez dos dados binários completos.

## Qual arquivo usar

Para explorar os registros individuais, use os arquivos em [normalized-data/registros](normalized-data/registros). Eles preservam as **591.760 linhas selecionadas**, incluindo as sinalizadas com problemas. As colunas `elegivel_painel` e `motivo_fora_painel` informam sua participação nas contagens.

Para explorar a agregação diária, consulte [normalized-data/conjuntos](normalized-data/conjuntos). Cada linha representa um par bairro e dia e contém a contagem `quantidade_registros`, além de informações de calendário. Não há médias móveis, defasagens, codificadores ou outras transformações aprendidas nestes arquivos. Para os experimentos de previsão semanal, use [normalized-data/modelagem](normalized-data/modelagem), seguindo as regras de disponibilidade e reconstrução de alvos descritas acima.

| Conjunto | Período da ocorrência | Linhas bairro e dia | Registros contabilizados no alvo |
|---|---|---:|---:|
| Treino | 01/01/2022 a 31/12/2023 | 1.362.180 | 305.143 |
| Validação | 01/01/2024 a 31/12/2024 | 645.990 | 126.885 |
| Teste | 01/01/2025 a 31/12/2025 | 636.195 | 107.297 |
| Avaliação adicional | 01/01/2026 a 30/06/2026 | 272.767 | 46.991 |
| Total | 01/01/2022 a 30/06/2026 | 2.917.132 | 586.316 |

As linhas de bairros de uma mesma data pertencem ao mesmo conjunto. Os arquivos estão ordenados por `data` e `bairro_chave`, sem embaralhamento e sem sobreposição de datas entre os conjuntos.

O teste de 2025 fica reservado para a avaliação após a escolha do modelo. A avaliação de 2026 é complementar e cobre somente meio ano. Os totais de 2025 e 2026 dependem dos registros tardios presentes no snapshot disponível.

## Tratamento aplicado

1. As colunas foram identificadas pelo nome, considerando mudanças de cabeçalho e posição entre os arquivos.
2. Foram selecionadas naturezas iniciadas por `ROUBO` e município da ocorrência igual a São Paulo. O município do registro não foi usado como filtro geográfico.
3. Datas foram convertidas e seus valores originais mantidos. A data da ocorrência define os conjuntos; a data de registro também foi preservada.
4. O bairro original foi mantido. A chave usada nas contagens tem espaços uniformizados, letras maiúsculas e acentos normalizados. Abreviações não foram expandidas automaticamente e não foram inventadas equivalências geográficas.
5. Coordenadas ausentes, zeradas, inválidas ou suspeitas foram sinalizadas. A falta de coordenadas não excluiu registros das contagens por bairro e dia.
6. Cada linha mantém arquivo, aba, número real da linha e identificador de proveniência. Os originais foram conferidos por SHA256 e permaneceram inalterados.

## Regras para inclusão nos painéis

Para entrar nas contagens, um registro precisa ter data de ocorrência e registro utilizáveis, bairro disponível e ocorrência entre 01/01/2022 e 30/06/2026. A ocorrência não pode ser posterior à data de registro. Reapresentações identificadas do mesmo BO e natureza não são contadas duas vezes.

| Motivo principal da não inclusão | Registros |
|---|---:|
| Data de ocorrência fora do intervalo | 2.928 |
| Bairro ausente | 2.299 |
| Data de ocorrência ausente ou inválida | 99 |
| Ocorrência posterior à data de registro | 116 |
| Reapresentação do mesmo BO e natureza | 2 |
| Total fora dos painéis | 5.444 |

Os motivos são aplicados em ordem e são mutuamente exclusivos no resumo. **591.760 preservados = 586.316 elegíveis + 5.444 fora dos painéis.** Todas essas linhas continuam nos Parquet de registros; o CSV de auditoria permite localizá-las.

As sete datas em texto com anos 0222 e 1202 foram preservadas para revisão e ficaram fora do período dos painéis. Nenhuma correção de ano foi feita por suposição.

## Revisão de boletins repetidos

Foram examinadas 12 linhas em seis grupos com a mesma delegacia, ano e número de BO. Em quatro grupos, as naturezas são distintas e foram mantidas. Nos outros dois, os registros repetem BO, natureza, data, bairro e hora em arquivos posteriores: a primeira data de registro foi mantida nas contagens, preservando todas as versões nos microdados.

A decisão e as referências de cada linha estão em [revisao_boletins.csv](normalized-data/auditoria/revisao_boletins.csv). A variável alvo conta registros por natureza criminal após essa revisão; ela não representa necessariamente crimes físicos únicos, vítimas ou taxas de risco.

## Cobertura e preenchimento de zeros

A verificação inicial havia sinalizado 141 linhas em 2022, uma em 2023 e duas em 2025. A revisão direta dos XML dos Excel confirmou que as **144 linhas eram vazias**, sem valores e sem ocorrências. Elas não representam registros estatísticos fora do período. As abas declaradas e todos os meses estatísticos esperados foram conferidos; para os três anos revisados, os meses também foram encontrados nos roubos selecionados da capital.

Com essa conferência, os conjuntos contêm todas as combinações de dias com os rótulos de bairro observados em cada ano. Pares sem registro elegível recebem zero. Para 2026, somente janeiro a junho é preenchido.

**Zero significa nenhum registro elegível encontrado neste snapshot**, não ausência de crime. A conferência comprova a cobertura declarada dos arquivos, sem garantir completude dos boletins ou existência de cada bairro em todos os dias.

## Cuidados para a futura modelagem

- A contagem alvo é retrospectiva e incorpora registros tardios. Para criar médias históricas e defasagens em uma previsão no início do dia, usar apenas registros cuja data de registro seja anterior ao dia previsto. Não calcular esses atributos diretamente do painel retrospectivo sem controlar a disponibilidade da informação.
- Não usar hora, coordenadas, natureza ou características de um fato futuro como entradas da previsão. Esses campos dos microdados descrevem fatos já registrados.
- Ajustar codificadores, imputações, seleção de atributos e parâmetros somente no treino. Usar a validação para escolher a configuração e reservar o teste para o resultado final.
- `bairro_visto_no_treino` identifica chaves presentes em 2022–2023. Existem rótulos novos na validação e no teste; o futuro modelo precisará tratá-los como categorias desconhecidas, sem ajustar o codificador com dados desses conjuntos.
- O universo de rótulos de cada ano é retrospectivo e não define territórios oficiais. Ao simular previsões reais, definir o cadastro de áreas disponível no momento da previsão ou apresentar resultados de bairros novos separadamente. Esses arquivos preparam a exploração e a engenharia de atributos, sem implementar essa política de produção.
- Os zeros são frequentes. Comparar os modelos com uma previsão de referência simples e analisar erros por bairro, além dos erros agregados.
- A implementação semanal usa somente SSP-SP. A construção de um mapa geográfico continua dependendo da correspondência entre rótulos de bairro e unidades espaciais; ela não é necessária para treinar os modelos tabulares.

## Formatos e conferência

Parquet pode ser lido por Python com pandas ou PyArrow, por R e por outras ferramentas de análise. Os CSV em `auditoria` podem ser consultados em planilhas. O [dicionário](normalized-data/metadados/dicionario.md) descreve os tipos e as sinalizações.

Foram conferidos os 591.760 identificadores de origem, a reconciliação de registros, as regras de elegibilidade, os totais dos quatro conjuntos, a ausência de pares bairro e dia repetidos, as contagens não negativas, a ordem das datas e a ausência de sobreposição entre os conjuntos. O [manifesto](normalized-data/metadados/manifest.json) contém as assinaturas das fontes e das saídas para verificar integridade.

Esta versão preserva o snapshot dos dados e acrescenta a preparação semanal. Os scripts e documentos anteriores de normalização permanecem no histórico do Git, incluindo o commit `7da66367c6eb1f269bc337d899e0a58ff41517f2`. A implementação dos modelos será adicionada em uma etapa posterior.
