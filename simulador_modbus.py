"""Simulador Modbus TCP, apenas com a biblioteca padrao do Python 3.

Exemplos:
    python simulador_modbus.py
    python simulador_modbus.py --dispositivos 255 --maximo 20000
    python simulador_modbus.py --passo 0 --valor0 123 --valor1 456

Por padrao sao simulados 128 dispositivos.
IDs 65..128 possuem somente os enderecos 81 e 82.
Esses IDs usam INT16 com sinal, na faixa -19999..19999 por padrao.
Os demais IDs possuem somente os enderecos 0 e 1.
As funcoes 03 e 04 leem os mesmos dois valores. Escritas nao sao aceitas.
Leituras individuais ou do bloco completo de 2 registradores sao aceitas.
Cada leitura respondida aparece imediatamente no terminal.
Requisicoes recebidas e respostas enviadas sao gravadas em log_modbus.csv
ao lado do script. Use --log-csv para escolher outro caminho.
Com passo > 0, ambos os valores avancam a cada intervalo, retornando ao
minimo de sua faixa assim que o proximo valor atingir ou ultrapassar o maximo.
"""

import argparse
import csv
from datetime import datetime
import logging
from pathlib import Path
import socketserver
import struct
import threading
import uuid


class LogCSV:
    campos = ["horario", "sessao", "conexao", "sequencia", "direcao",
              "cliente_ip", "cliente_porta", "transacao", "slave", "funcao",
              "endereco_inicial", "quantidade", "valores", "status",
              "excecao", "mensagem", "pacote_hex"]

    def __init__(self, caminho):
        self.caminho = Path(caminho).expanduser().resolve()
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.sessao = uuid.uuid4().hex
        self.lock = threading.Lock()
        # Acrescenta ao historico, sem apagar execucoes anteriores.
        with self.caminho.open("a", encoding="utf-8-sig", newline="") as arquivo:
            if arquivo.tell() == 0:
                csv.DictWriter(arquivo, fieldnames=self.campos, delimiter=";").writeheader()

    def registrar(self, conexao, sequencia, direcao, cliente, transacao,
                  dispositivo, pdu, pacote, endereco="", quantidade="",
                  status="OK", mensagem=""):
        valores = ""
        excecao = ""
        if direcao == "ENVIADO" and pdu:
            if pdu[0] & 0x80:
                excecao = f"0x{pdu[1]:02X}"
                if status == "OK":
                    status = "EXCECAO_MODBUS"
            elif pdu[0] in (3, 4):
                formato = "h" if Banco.assinado(dispositivo) else "H"
                numeros = struct.unpack(">" + formato * (pdu[1] // 2), pdu[2:])
                valores = " | ".join(f"R{endereco + i}={v}" for i, v in enumerate(numeros))
        linha = dict(zip(self.campos, [
            datetime.now().astimezone().isoformat(timespec="milliseconds"),
            self.sessao, conexao, sequencia, direcao, cliente[0], cliente[1],
            transacao, dispositivo, pdu[0] if pdu else "", endereco,
            quantidade, valores, status, excecao, mensagem, pacote.hex(" ").upper()
        ]))
        # Escrita serializada entre clientes. Fechar apos cada linha torna os
        # registros disponiveis imediatamente e evita buffers pendentes.
        with self.lock:
            with self.caminho.open("a", encoding="utf-8-sig", newline="") as arquivo:
                csv.DictWriter(arquivo, fieldnames=self.campos, delimiter=";").writerow(linha)


class Banco:
    def __init__(self, quantidade, maximo, valor0, valor1,
                 minimo_assinado=-19999, maximo_assinado=19999,
                 valor81=None, valor82=None):
        self.maximo = maximo
        self.minimo_assinado = minimo_assinado
        self.maximo_assinado = maximo_assinado
        valores_assinados = [valor0 if valor81 is None else valor81,
                            valor1 if valor82 is None else valor82]
        self.registradores = {
            i: list(valores_assinados) if self.assinado(i) else [valor0, valor1]
            for i in range(1, quantidade + 1)
        }
        self.lock = threading.Lock()

    def ler(self, dispositivo, endereco, quantidade):
        indice = endereco - self.endereco_inicial(dispositivo)
        with self.lock:
            return self.registradores[dispositivo][indice:indice + quantidade]

    @staticmethod
    def assinado(dispositivo):
        return 65 <= dispositivo <= 128

    @staticmethod
    def endereco_inicial(dispositivo):
        return 81 if Banco.assinado(dispositivo) else 0

    def incrementar(self, passo):
        if passo == 0:
            return
        with self.lock:
            for dispositivo, valores in self.registradores.items():
                minimo, maximo = ((self.minimo_assinado, self.maximo_assinado)
                                  if self.assinado(dispositivo) else (0, self.maximo))
                for i in range(2):
                    proximo = valores[i] + passo
                    valores[i] = minimo if proximo >= maximo else proximo

    def responder(self, dispositivo, pdu):
        funcao = pdu[0]
        if dispositivo not in self.registradores:
            return bytes([funcao | 0x80, 0x0B])
        if funcao not in (3, 4):
            return bytes([funcao | 0x80, 0x01])
        if len(pdu) != 5:
            return bytes([funcao | 0x80, 0x03])
        endereco, quantidade = struct.unpack(">HH", pdu[1:])
        if not 1 <= quantidade <= 125:
            return bytes([funcao | 0x80, 0x03])
        base = self.endereco_inicial(dispositivo)
        if endereco < base or endereco + quantidade > base + 2:
            return bytes([funcao | 0x80, 0x02])
        valores = self.ler(dispositivo, endereco, quantidade)
        # Modbus transporta palavras de 16 bits; negativos usam complemento de dois.
        return bytes([funcao, 2 * quantidade]) + struct.pack(
            ">" + "H" * quantidade, *(valor & 0xFFFF for valor in valores)
        )


def receber_exatamente(sock, tamanho):
    dados = bytearray()
    while len(dados) < tamanho:
        bloco = sock.recv(tamanho - len(dados))
        if not bloco:
            return None
        dados.extend(bloco)
    return bytes(dados)


class Conexao(socketserver.BaseRequestHandler):
    def handle(self):
        conexao = uuid.uuid4().hex
        sequencia = 0

        def registrar(direcao, pdu, pacote, endereco="", quantidade="", status="OK", mensagem=""):
            if self.server.log_csv is not None:
                self.server.log_csv.registrar(
                    conexao, sequencia, direcao, self.client_address, transacao,
                    dispositivo, pdu, pacote, endereco, quantidade, status, mensagem
                )

        try:
            while True:
                cabecalho = receber_exatamente(self.request, 7)
                if cabecalho is None:
                    return
                transacao, protocolo, tamanho, dispositivo = struct.unpack(">HHHB", cabecalho)
                sequencia += 1
                # Comprimento MBAP inclui Unit ID e PDU (maximo de 253 bytes).
                if protocolo != 0 or not 2 <= tamanho <= 254:
                    registrar("RECEBIDO", b"", cabecalho, status="CABECALHO_INVALIDO",
                              mensagem="Conexao encerrada: protocolo ou comprimento MBAP invalido")
                    return
                pdu = receber_exatamente(self.request, tamanho - 1)
                if pdu is None:
                    return
                endereco, quantidade = "", ""
                if pdu[0] in (3, 4) and len(pdu) == 5:
                    endereco, quantidade = struct.unpack(">HH", pdu[1:])
                registrar("RECEBIDO", pdu, cabecalho + pdu, endereco, quantidade)
                resposta = self.server.banco.responder(dispositivo, pdu)
                mbap = struct.pack(">HHHB", transacao, 0, len(resposta) + 1, dispositivo)
                try:
                    self.request.sendall(mbap + resposta)
                except OSError as erro:
                    registrar("ENVIADO", resposta, mbap + resposta, endereco, quantidade,
                              status="FALHA_ENVIO", mensagem=f"Envio incompleto ou nao realizado: {erro}")
                    raise
                registrar("ENVIADO", resposta, mbap + resposta, endereco, quantidade)
                if not resposta[0] & 0x80:
                    endereco = struct.unpack(">H", pdu[1:3])[0]
                    formato = "h" if self.server.banco.assinado(dispositivo) else "H"
                    valores = struct.unpack(">" + formato * (resposta[1] // 2), resposta[2:])
                    detalhes = " | ".join(
                        f"R{endereco + i}={valor}" for i, valor in enumerate(valores)
                    )
                    logging.info("LEITURA | ID=%03d | FC=%02d | %s",
                                 dispositivo, pdu[0], detalhes)
                else:
                    logging.warning("ERRO | ID=%03d | FC=%02d | excecao=0x%02X",
                                    dispositivo, pdu[0], resposta[1])
        except OSError as erro:
            # Desconectar/reconectar um cliente nao encerra o servidor.
            logging.warning("Conexao %s:%s encerrada: %s", *self.client_address, erro)
            return


class Servidor(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, endereco, banco, log_csv=None):
        self.banco = banco
        self.log_csv = log_csv
        super().__init__(endereco, Conexao)


def argumentos():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", type=int, default=501)
    parser.add_argument("--dispositivos", type=int, default=128)
    parser.add_argument("--maximo", type=int, default=9999, help="Maximo dos IDs fora de 65..128 (UINT16).")
    parser.add_argument("--minimo-assinado", type=int, default=-19999, help="Minimo dos IDs 65..128.")
    parser.add_argument("--maximo-assinado", type=int, default=19999, help="Maximo dos IDs 65..128.")
    parser.add_argument("--passo", type=int, default=1, help="Incremento de cada registrador; 0 mantém valores fixos.")
    parser.add_argument("--intervalo", type=float, default=1.0, help="Segundos entre incrementos.")
    parser.add_argument("--valor0", type=int, default=0, help="Valor inicial de R0 ou R81, conforme o ID.")
    parser.add_argument("--valor1", type=int, default=0, help="Valor inicial de R1 ou R82, conforme o ID.")
    parser.add_argument("--valor81", type=int, help="Valor inicial de R81; padrao: --valor0.")
    parser.add_argument("--valor82", type=int, help="Valor inicial de R82; padrao: --valor1.")
    parser.add_argument("--log-csv", default=str(Path(__file__).resolve().with_name("log_modbus.csv")),
                        help="Caminho do CSV; padrao: log_modbus.csv ao lado do script.")
    args = parser.parse_args()
    if not 1 <= args.dispositivos <= 255:
        parser.error("--dispositivos deve estar entre 1 e 255")
    if not 0 <= args.maximo <= 65535:
        parser.error("--maximo deve estar entre 0 e 65535 (registrador de 16 bits)")
    if not -32768 <= args.minimo_assinado <= args.maximo_assinado <= 32767:
        parser.error("Faixa assinada deve respeitar -32768 <= minimo <= maximo <= 32767")
    if not 1 <= args.porta <= 65535:
        parser.error("--porta deve estar entre 1 e 65535")
    if args.passo < 0:
        parser.error("--passo deve ser maior ou igual a zero")
    if not 0 < args.intervalo < float("inf"):
        parser.error("--intervalo deve ser finito e maior que zero")
    if not all(0 <= v <= args.maximo for v in (args.valor0, args.valor1)):
        parser.error("--valor0 e --valor1 devem estar entre 0 e --maximo")
    args.valor81 = args.valor0 if args.valor81 is None else args.valor81
    args.valor82 = args.valor1 if args.valor82 is None else args.valor82
    if args.dispositivos >= 65 and not all(
        args.minimo_assinado <= v <= args.maximo_assinado for v in (args.valor81, args.valor82)
    ):
        parser.error("--valor81 e --valor82 devem estar dentro da faixa assinada")
    return args


def main():
    args = argumentos()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    banco = Banco(args.dispositivos, args.maximo, args.valor0, args.valor1,
                  args.minimo_assinado, args.maximo_assinado, args.valor81, args.valor82)
    try:
        log_csv = LogCSV(args.log_csv)
    except OSError as erro:
        raise SystemExit(f"Nao foi possivel abrir o log CSV: {erro}") from erro
    try:
        servidor = Servidor((args.host, args.porta), banco, log_csv)
    except OSError as erro:
        raise SystemExit(f"Nao foi possivel abrir {args.host}:{args.porta}: {erro}") from erro
    parar = threading.Event()

    def atualizar():
        while not parar.wait(args.intervalo):
            banco.incrementar(args.passo)

    worker = threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    worker.start()
    atualizador = None
    if args.passo:
        atualizador = threading.Thread(target=atualizar, daemon=True)
        atualizador.start()
    logging.info("Servidor em %s:%s | IDs 1..%s", args.host, args.porta, args.dispositivos)
    logging.info("Log CSV: %s | historico preservado | separador: ponto e virgula", log_csv.caminho)
    logging.info("IDs 65..128: R81/R82 INT16 | faixa %s..%s | iniciais [%s, %s]",
                 args.minimo_assinado, args.maximo_assinado, args.valor81, args.valor82)
    logging.info("Demais IDs: R0/R1 UINT16 | faixa 0..%s | iniciais [%s, %s]",
                 args.maximo, args.valor0, args.valor1)
    logging.info("Passo: %s | intervalo: %ss | Ctrl+C encerra", args.passo, args.intervalo)
    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        logging.info("Encerrando servidor...")
    finally:
        parar.set()
        servidor.shutdown()
        servidor.server_close()
        worker.join()
        if atualizador is not None:
            atualizador.join()


if __name__ == "__main__":
    main()
