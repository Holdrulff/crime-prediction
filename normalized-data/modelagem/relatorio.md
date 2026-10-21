# Conferência da base semanal

| Conjunto | Semanas | Exemplos por janela | Registros avaliados | Registros de bairros desconhecidos |
|---|---:|---:|---:|---:|
| avaliacao_2026 | 25 | 58876 | 46059 | 48 |
| teste | 51 | 116436 | 105216 | 92 |
| treino | 92 | 170024 | 268270 | 858 |
| validacao | 52 | 113661 | 126220 | 115 |

Reconciliação: 586316 elegíveis = 545765 avaliados + 1113 de bairros desconhecidos + 39438 fora do calendário.

As três janelas têm os mesmos pares bairro–semana. Alvos retrospectivos servem à avaliação; reconstruir alvos de treinamento com o subcomando targets.

Não há treinamento de modelos. Cadastro de bairros e entradas respeitam data_registro anterior à previsão. Data de registro não é data comprovada de publicação. Os alvos recentes podem estar incompletos por atraso e pelo término do snapshot.
