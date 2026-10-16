# Dicionário dos dados tratados

## Registros selecionados

| Campo | Tipo | Significado |
|---|---|---|
| record_id | string | SHA256 de arquivo original, aba e número real da linha; identifica proveniência, não uma ocorrência única. |
| arquivo_origem | string | Nome do XLSX de origem. |
| aba_origem | string | Nome real da aba. |
| delegacia | string | Valor original da SSP; origem: NOME_DELEGACIA. |
| municipio_registro | string | Valor original da SSP; origem: NOME_MUNICIPIO ou CIDADE. |
| numero_bo | string | Valor original da SSP; origem: NUM_BO. |
| ano_bo | string | Valor original da SSP; origem: ANO_BO. |
| data_registro_raw | string | Valor original da célula, antes da conversão; datas numéricas são seriais do Excel. |
| data_ocorrencia_raw | string | Valor original da célula, antes da conversão; datas numéricas são seriais do Excel. |
| hora_ocorrencia_raw | string | Valor original da célula, antes da conversão; datas numéricas são seriais do Excel. |
| bairro_original | string | Texto da SSP preservado; não é uma unidade territorial oficial. |
| bairro_normalizado | string | Espaços uniformes, maiúsculas e acentos removidos; NULL quando ausente. |
| bairro_chave | string | Nome normalizado ou destino de equivalência revisada; unidade textual usada na agregação. |
| municipio_ocorrencia | string | Valor original da SSP; origem: NOME_MUNICIPIO_CIRCUNCRICAO ou NOME_MUNICIPIO_CIRCUNSCRICAO. |
| natureza | string | Valor original da SSP; origem: NATUREZA_APURADA. |
| rubrica | string | Valor original da SSP; origem: RUBRICA. |
| conduta | string | Valor original da SSP; origem: DESCR_CONDUTA. |
| subtipo_local | string | Valor original da SSP; origem: DESCR_SUBTIPOLOCAL. |
| latitude_raw | string | Valor original da célula, antes da conversão; datas numéricas são seriais do Excel. |
| longitude_raw | string | Valor original da célula, antes da conversão; datas numéricas são seriais do Excel. |
| coordenadas_status | string | ausente, invalida, zero, fora_limites_numericos, suspeita_bbox ou plausivel. Bbox não confirma limites municipais. |
| bo_chave | string | SHA256 de delegacia normalizada, ano e número do BO. Chave candidata, não prova de unicidade. |
| row_fingerprint | string | SHA256 de todos os campos originais da linha com cabeçalhos normalizados. Não exclui registros. |
| linha_origem | int32 | Número real da linha no Excel, começando em 1. |
| ano_arquivo | int16 | Ano no nome do arquivo; não substitui o ano da ocorrência. |
| data_ocorrencia | date32[day] | Data do fato; sem data válida não entra no painel diário. |
| data_registro | date32[day] | Data do registro/comunicação. Preservar para controlar disponibilidade histórica antes de prever. |
| data_ocorrencia_status | string | valida, ausente ou invalida. |
| data_registro_status | string | valida, ausente ou invalida. |
| ocorrencia_posterior_registro | bool | Sinalização temporal; linha preservada para revisão. |
| atraso_registro_dias | int32 | Data de registro menos data de ocorrência; negativo indica inconsistência. |
| bairro_ausente | bool | Indica ausência de bairro conforme sentinelas nas regras. |
| latitude | double | Latitude convertida; avaliar em conjunto com coordenadas_status. |
| longitude | double | Longitude convertida; avaliar em conjunto com coordenadas_status. |

## Painéis por bairro e dia

| Campo | Tipo | Significado |
|---|---|---|
| bairro_chave | string | Nome normalizado ou destino de equivalência revisada; unidade textual usada na agregação. |
| data | date32[day] | Dia da ocorrência representado na agregação. |
| quantidade_registros | int32 | Contagem de linhas selecionadas preservadas. Zero é ausência de registros na base coberta, não ausência de crime. |
| dia_semana | int8 | 0=segunda, 1=terça, ..., 6=domingo. |
| mes | int8 | Mês de 1 a 12. |
| ano | int16 | Ano da ocorrência. |
| cobertura_confirmada | bool | Abas correspondem ao intervalo declarado e meses estatísticos esperados estão presentes, sem registros estatísticos fora do intervalo. |
| periodo_parcial | bool | True para cobertura inferior ao ano completo; True em 2026. |

## Campos adicionais dos registros

| Campo | Tipo | Significado |
|---|---|---|
| elegivel_painel | bool | Linha usada nas contagens dos conjuntos. |
| motivo_fora_painel | string | Motivo principal da não inclusão; NULL para elegíveis. A linha é preservada. |

## Conjuntos temporais

Os conjuntos contêm data, bairro_chave, quantidade_registros, dia_semana, mes, ano, bairro_visto_no_treino, cobertura_confirmada e periodo_parcial. Não contêm atributos históricos, preenchimentos estatísticos ou codificadores ajustados.

bairro_visto_no_treino indica se a chave ocorreu nos registros elegíveis de 2022–2023. É um diagnóstico de categorias novas; não é um atributo calculado a partir do alvo futuro.

O universo de rótulos de cada ano é retrospectivo. O preenchimento zero expressa ausência de registros elegíveis no snapshot, não ausência de crime nem garantia de existência do bairro em todos os dias.

Nos microdados, status de data valida significa data conversível, não plausibilidade histórica confirmada. Anos anômalos ficam preservados e fora dos painéis.
