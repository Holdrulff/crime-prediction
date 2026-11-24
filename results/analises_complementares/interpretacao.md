# Leitura científica das análises complementares

Estas análises aprofundam o experimento existente. Foram formuladas depois de
conhecidos os resultados principais; não constituem confirmação independente.
Os intervalos são exploratórios e não corrigidos por multiplicidade. A seleção
original e suas previsões permanecem preservadas.

## O que a análise por volume acrescenta

Os quartis foram reconstruídos usando somente o volume disponível no treino do
teste de 2025. Seus limites são 2, 12,5 e 45 registros no período de ajuste. Empates
explicam os tamanhos desiguais dos grupos. Esses números são contagens históricas,
não limites de risco ou taxas por população.

No Q1, 99,27% dos pares têm alvo zero. O MAE da previsão zero é 0,007551, diante
de 0,014457 da média de quatro semanas, 0,025196 do RF e 0,062744 do XGBoost.
Isso ilustra por que um MAE baixo não significa prever bem os registros positivos:
a estratégia zero deixa de prever todos eles. No Q1, o XGBoost tem menor MAE que a
média no subconjunto retrospectivo positivo (0,970694 contra 1,013830), mas maior
erro nos zeros (0,056065 contra 0,007106), que são muito mais frequentes.

No Q4, 557 rótulos de bairro somam 28.407 pares e 99.637 registros observados,
94,7% dos 105.216 registros avaliados. Ainda assim, 45,49% dos pares desse grupo
têm alvo zero. O MAE é 1,327287 para a média, 1,300494 para RF e 1,284257 para
XGBoost. As diferenças modelo menos média são -0,026793 e -0,043031, com intervalos
exploratórios de 95% de [-0,046015; -0,009542] e [-0,065118; -0,023590].

O erro médio com sinal no Q4 é -0,261986 para a média, +0,050402 para RF e
+0,028390 para XGBoost, por par bairro e semana. A média subestima o total do
grupo; os modelos ficam mais próximos de zero, com superestimação média. Essa proximidade do
total não substitui o MAE: erros opostos podem se cancelar.

Separando retrospectivamente os pares do Q4, os MAEs nas semanas positivas são
2,033131, 1,934204 e 1,943366 para média, RF e XGBoost. Nos zeros, são 0,481564,
0,541200 e 0,494529. Assim, nesse grupo os modelos reduzem o erro nas semanas
positivas e aumentam o erro nos zeros em relação à média. É uma decomposição
descritiva do desempenho observado, não uma explicação causal e não uma regra
de escolha que possa conhecer previamente o alvo da semana.

O Q4 concentra aproximadamente 96,5% a 96,6% do erro quadrático total desses três
métodos. Isso localiza onde se concentra a penalização que entra no RMSE.
Nas faixas Q1 a Q3, ambos os modelos têm MAE maior que a média; os ganhos do Q4
não compensam essas perdas no MAE global, calculado por par bairro e semana.

O grupo sem histórico de ajuste contém 99 rótulos de bairro, 2.604 pares e apenas
17 registros observados. O intervalo da diferença RF menos média cruza zero;
não há suporte para uma conclusão forte a partir dessa pequena população. Os
rótulos não são necessariamente territórios oficiais ou bairros recém-criados.

## Como interpretar a remoção de atributos

As comparações mantêm a configuração selecionada para a versão completa. Portanto,
respondem ao valor de cada grupo nessa configuração e nessa validação, não ao
melhor desempenho possível de cada conjunto de entradas. A configuração completa
já foi escolhida em 2024, o que pode favorecê-la nesta análise.

Remover bairro testa o valor de sua identificação textual, não relações de
vizinhança. Remover calendário testa somente o mês da segunda-feira prevista.
Remover ambos conjuntamente não isola uma interação estatística. A retirada de
resumos não identifica separadamente o efeito de cada um dos quatro atributos.

Média, desvio padrão e tendência são derivados dos blocos semanais. A quantidade
de dias sem registros usa informação diária que não pode, em geral, ser recuperada
apenas dos totais semanais. Atributos correlacionados podem substituir parte da
informação uns dos outros. Nenhuma remoção demonstra uma causa da criminalidade.

Um adendo técnico registrado antes dos ajustes afetados manteve matrizes CSR em
todas as versões, inclusive a que remove todas as categorias. Isso conserva a
representação esparsa do experimento original para o XGBoost, no qual entradas
não armazenadas são tratadas como ausentes. A finalidade é não alterar também essa
semântica ao retirar atributos. O código e o registro iniciais estão preservados.

## Resultados da contribuição dos atributos em 2024

Todos os dez ajustes usaram 161.558 exemplos no treino e os mesmos 104.714 pares
em 48 semanas de validação. A diferença máxima entre a versão completa e a previsão
original foi 1,42 × 10⁻¹⁴ para RF e zero para XGBoost. As matrizes de entrada completas
também foram comparadas diretamente às originais e são idênticas.

| Versão | MAE RF | RMSE RF | MAE XGBoost | RMSE XGBoost |
|---|---:|---:|---:|---:|
| Completa | 0,472923 | 1,432973 | 0,481137 | 1,422726 |
| Sem bairro | 0,476131 | 1,433664 | 0,479721 | 1,425731 |
| Sem calendário | 0,490766 | 1,435936 | 0,482553 | 1,432199 |
| Sem resumos | 0,473260 | 1,427124 | 0,481372 | 1,431366 |
| Somente histórico e resumos | 0,476971 | 1,434371 | 0,481493 | 1,438404 |

No RF, a retirada do mês produziu a maior piora de MAE entre as remoções estudadas:
+0,017843, com intervalo exploratório de 95% de [+0,015518; +0,019603]. Retirar o
bairro produziu +0,003208 [+0,001693; +0,004379]. Isso sustenta uma contribuição
preditiva condicional dessas entradas para essa configuração e esse período,
sem provar efeito causal ou generalização para outro ano.

Retirar os resumos do RF alterou pouco o MAE: +0,000337, com intervalo
[-0,001186; +0,001773], abrangendo zero. O RMSE, por sua vez, diminuiu de 1,432973
para 1,427124. Portanto, esses dados não sustentam uma afirmação de benefício
uniforme dos resumos nas duas métricas. A remoção conjunta de bairro e mês
produziu +0,004048 [+0,001164; +0,006314], sem permitir isolar uma interação.

No XGBoost, retirar o bairro reduziu pontualmente o MAE em 0,001417, mas o intervalo
[-0,002417; +0,000309] abrange zero, e o RMSE aumentou. Não se deve anunciar uma
melhoria robusta ou escolher uma nova versão com esse resultado. A retirada do mês
elevou o MAE em 0,001416 [+0,000183; +0,002155]. As diferenças de MAE sem resumos
e somente com histórico e resumos foram pequenas e seus intervalos abrangeram zero.

O MAE da média de quatro semanas nessa validação é 0,469178, menor que o de todas
as versões treinadas. A análise explica diferenças internas entre representações,
mas não reverte a vantagem da referência no critério principal. Não foram abertas
novas buscas de parâmetros nem executadas essas versões em 2025 ou 2026.

## Consequência para o artigo final

A contribuição do aprofundamento é mostrar que os grupos de entradas não têm
efeitos uniformes entre famílias e métricas, e localizar o contraste entre erros
nos zeros e nos registros positivos. A conclusão original sobre MAE global e RMSE
permanece. Confirmação em outro período e comparação entre sementes são etapas
futuras. Tempos de execução são observações desta máquina e desta execução, não
estimativas universais de desempenho computacional.

Os valores completos, intervalos e denominadores estão em atributos/metricas.csv,
atributos/contrastes.csv, volume/metricas_por_volume.csv e volume/contrastes.csv.
O registro inicial, o adendo, as verificações independentes e os manifestos permitem
auditar a sequência das decisões e a preservação dos resultados originais.
