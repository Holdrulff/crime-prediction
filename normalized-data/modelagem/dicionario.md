# Dicionário dos exemplos semanais

## Unidade e elegibilidade

Uma linha por bairro conhecido no início da segunda-feira e semana prevista completa (segunda a domingo). As três janelas usam o mesmo calendário, após 84 dias de cobertura. O cadastro cresce com a primeira data de registro elegível estritamente anterior à previsão; não usa rótulos descobertos no futuro e não exige que o bairro permaneça ativo. As exclusões dos microdados normalizados continuam aplicadas.

## Exemplos

| Campo | Tipo | Significado |
|---|---|---|
| bairro_chave | string | Rótulo textual SSP normalizado; categoria conhecida antes da previsão, sem polígono ou vizinhança oficial. |
| inicio_semana | date32 | Segunda-feira da previsão, no início do dia. Identificador temporal, não preditor direto. |
| fim_semana | date32 | Domingo seguinte, incluído no alvo. |
| janela_dias | int16 | 28, 56 ou 84; configuração do experimento. |
| mes | int8 | Mês da segunda-feira, conhecido antecipadamente. |
| contagem_semana_anterior_01 ... | int32 | Contagens disponíveis de ocorrências em blocos de sete dias; 01 é o bloco mais recente, [início−7, início). Há 4, 8 ou 12 blocos. |
| media_semanal | float64 | Média das contagens nos blocos da janela. |
| desvio_padrao_semanal | float64 | Desvio padrão populacional dos blocos, divisor n (ddof=0). |
| tendencia_semanal | float64 | Inclinação por mínimos quadrados das contagens semanais, em ordem cronológica; unidade registros por semana de avanço. |
| dias_sem_registros | int16 | Dias com zero registros disponíveis na janela. Não significa ausência de crimes. |
| alvo_retrospectivo | int32 | Contagem da semana no snapshot completo disponível. Exclusivamente para avaliação, não para ajuste histórico. |

Todos os atributos históricos exigem data_ocorrencia dentro da janela e data_registro anterior ao início da semana. Zeros anteriores à primeira observação de um bairro não comprovam existência territorial. Não há codificadores ajustados, seleção aprendida, vizinhos geográficos, IBGE ou modelos treinados.

## Reconstrução dos alvos

contribuicoes_alvo.parquet: bairro_chave (string), inicio_semana (date32), data_registro (date32) e quantidade_registros (int32). Soma de contribuições por bairro e semana recupera o alvo retrospectivo. O comando targets soma somente data_registro < data de corte, remove alvo_retrospectivo e semanas ainda abertas, e acrescenta alvo_disponivel (int32) e data_corte_treinamento (date32). Entradas históricas permanecem as calculadas na origem de cada exemplo. O corte é exclusivo, inclusive para semanas e registros na fronteira. Alvos disponíveis são provisórios: ausência de registro na data de corte não comprova ausência de ocorrência. Não há política de maturação imposta nesta preparação.

## Auditoria

bairros_desconhecidos.parquet: bairro_chave (string), inicio_semana (date32) e quantidade_registros (int32) dos alvos de categorias desconhecidas na origem, sem inseri-las artificialmente nas entradas. relatorio.json contém totais por conjunto, semanas e exclusões do calendário. conferencia_semanal.csv detalha os mesmos totais por semana. Cada contagem é reportada uma vez, sem triplicar os registros pelas três janelas.

O manifesto semanal cobre os arquivos desta pasta, exceto ele próprio. O manifesto original continua cobrindo o snapshot anterior preservado. A data de registro é proxy da publicação; não existem snapshots históricos completos das versões dos boletins. Alvos recentes podem estar incompletos por registros tardios fora do snapshot.
