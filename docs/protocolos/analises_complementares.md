# Protocolo das análises complementares

## Registro e posição no estudo

Este protocolo é registrado antes de executar as análises complementares. O comando
`register` grava a data UTC, a configuração, os parâmetros previamente selecionados,
as versões e as assinaturas de fontes, código e resultados originais. Não constitui
pré-registro independente: as perguntas foram formuladas após conhecer 2025 e 2026.
Os resultados originais não serão substituídos nem usados para novas escolhas.

## Contribuição dos grupos de atributos

Pergunta: quanto remover cada representação altera o desempenho das configurações
já selecionadas? RF e XGBoost manterão seus parâmetros, 84 dias, semente 42, no máximo
quatro threads e execução sequencial. Serão dez ajustes: completa; sem bairro;
sem calendário; somente histórico e resumos; sem resumos. Histórico são os 12 blocos
anteriores de sete dias; resumos são média, desvio padrão, tendência e dias sem
registros; calendário é mês; bairro é o rótulo SSP codificado no ajuste.

Usaremos exatamente a preparação original de validação: ajuste no corte exclusivo
01/01/2024, alvos reconstruídos, maturação de 28 dias; avaliação em 48 semanas de
2024, com alvos disponíveis antes de 01/01/2025 e maturação. Os pares e alvos serão
idênticos entre versões. Não serão treinadas ablações em 2025/2026. O codificador
verá apenas o ajuste. A versão completa deverá reproduzir as previsões originais
com rtol=1e-6 e atol=1e-7; divergência interrompe as comparações da família.

Métricas: MAE e RMSE por versão e comparação com média de quatro semanas.
Delta MAE é versão reduzida menos completa, positivo indica piora. Para oito
contrastes, intervalos percentis exploratórios de 95%, 1.000 reamostragens pareadas
em blocos móveis de quatro semanas avaliadas, incluindo todos os bairros dessas
semanas, semente 42 e os mesmos sorteios entre contrastes. Não serão ajustados
parâmetros nem escolhida nova versão vencedora. Não haverá alegação confirmatória
ou de significância sem controle da multiplicidade. A validação já orientou a
configuração completa, podendo favorecê-la. A análise não mede causalidade, valor
universal dos atributos, nem robustez entre sementes. Atributos são correlacionados
e resumos derivados podem ser parcialmente redundantes; remoção conjunta não isola
uma interação. A redução das colunas também altera o espaço de divisões das árvores.

## Erros por volume

Usaremos somente as previsões originais de 2025, dos cinco métodos. Os quartis serão
os do volume somado dos alvos disponíveis no ajuste de 2025, com cortes exclusivamente
no treino. Valores empatados ficam na mesma faixa; grupos podem ser desiguais ou
vazios. Bairros sem histórico de ajuste ficam separados. Reconstruiremos e
conferiremos os rótulos dos grupos contra os arquivos originais.

Por grupo e método: bairros, pares, semanas com dados, total observado, proporção
de alvos zero, MAE, RMSE, erro médio com sinal (previsão menos observado), soma dos
erros absolutos e quadráticos e suas participações no erro total daquele método.
Também separaremos retrospectivamente alvo zero/positivo, sem tratá-los como
grupos conhecidos antes de prever. Intervalos exploratórios de delta MAE de RF
e XGBoost menos média serão calculados nos quartis e no grupo desconhecido usando
o calendário completo de 51 semanas e os mesmos blocos para todos os contrastes.
Semanas sem pares em um grupo têm soma e denominador zero, não erro zero. Menos de
quatro semanas com dados impede intervalo; amostras de denominador zero são
descartadas e contabilizadas. Denominadores de participação são de cada modelo.

A avaliação original não é um novo teste independente desta hipótese. Intervalos
não serão usados para escolher um modelo por bairro. Grupos por alvo observado
não receberão inferência confirmatória. A dependência espacial e a escolha de
quatro semanas limitam os intervalos. Os blocos seguem a ordem de semanas elegíveis;
eventuais intervalos excluídos do calendário serão informados.

## Integridade, saídas e encerramento

Saídas em `results/analises_complementares/`: registro e manifesto de preservação,
previsões por versão, métricas CSV, contrastes e intervalos, composição dos grupos,
diagnósticos por semana/bairro, gráficos PNG/PDF e relatório Markdown.
Originais serão conferidos por SHA256 antes e depois. Execuções parciais só serão
retomadas se código, configuração, protocolo e fontes continuarem idênticos.
Os testes cobrem máscaras, categorias futuras, métricas manuais, pares, quartis,
bootstrap e reconciliação. Os testes temporais originais também serão executados.

Encerramento: dez ajustes completos, reprodução das versões completas, saídas
reconciliadas, gráficos inspecionados e relatório sem alegação de confirmação
independente. Não se inclui nova busca, treinamento em 2025, perdas alternativas,
integração geográfica, edição do paper ou submissão na disciplina nesta execução.
