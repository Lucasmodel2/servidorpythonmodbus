# servidorpythonmodbus

Simulador Modbus TCP em Python.

Servidor Python 3 sem dependencias externas, em `127.0.0.1:501` por padrao.

## Executar

```powershell
python simulador_modbus.py
```

No Windows, tambem pode ser usado `py` em vez de `python`.

Simula 128 slaves por padrao, configuraveis de 1 a 255:

| Slaves | Registradores | Tipo | Faixa padrao |
| --- | --- | --- | --- |
| 1 a 64 | 0 e 1 | UINT16 | 0 a 9999 |
| 65 a 128 | 81 e 82 | INT16 com sinal | -19999 a 19999 |
| 129 a 255, quando habilitados | 0 e 1 | UINT16 | 0 a 9999 |

Aceita leituras individuais e em bloco pelas funcoes 03 e 04. Nao aceita escrita.
Os valores iniciam em zero e avancam 1 a cada segundo. Quando o incremento
atinge ou ultrapassa o maximo, cada registrador retorna ao minimo da sua faixa.

## Exemplos

```powershell
python simulador_modbus.py --dispositivos 255
python simulador_modbus.py --passo 10 --intervalo 2
python simulador_modbus.py --valor81 -19999 --valor82 -19999
python simulador_modbus.py --passo 0 --valor0 100 --valor1 200 --valor81 -100 --valor82 200
python simulador_modbus.py --maximo 15000 --minimo-assinado -19999 --maximo-assinado 19999
python simulador_modbus.py --log-csv "logs\comunicacao.csv"
python simulador_modbus.py --help
```

Encerre com Ctrl+C antes de iniciar outra instancia na mesma porta.

## Logs

Mostra as leituras no terminal e grava requisicoes recebidas e respostas enviadas
em `log_modbus.csv`, ao lado do script. O CSV usa ponto e virgula como separador,
preserva o historico e inclui horarios, IDs, valores e pacotes em hexadecimal.
O registro do envio nao confirma o processamento da resposta pelo cliente.

Os arquivos CSV sao ignorados pelo Git.
