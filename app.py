"""
Sabor de Casa — Sistema de delivery para restaurante / lanchonete.

Como usar:
    1) Tenha o MySQL instalado e rodando
    2) Preencha usuário e senha do MySQL no arquivo config.ini
    3) python -m pip install -r requirements.txt
    4) python app.py  -> o navegador abre em http://localhost:5000

Login do dono (painel): admin@delivery.com / admin123  (troque depois!)
"""
import configparser
import os
import secrets
import sys
import threading
import webbrowser
from datetime import datetime
from decimal import Decimal
from functools import wraps


def _instalar_o_que_falta():
    """Instala Flask/PyMySQL no MESMO Python que está rodando o sistema, se faltarem.

    Evita o erro "No module named 'pymysql'" quando o VS Code (ou o Windows)
    usa um Python diferente daquele onde os pacotes foram instalados.
    """
    import importlib
    import subprocess
    faltando = []
    for modulo in ("flask", "pymysql", "cryptography"):
        try:
            importlib.import_module(modulo)
        except ImportError:
            faltando.append(modulo)
    if not faltando:
        return
    req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    print("Instalando o que falta (" + ", ".join(faltando) + ") — só na primeira vez...")
    print("Python usado:", sys.executable)
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", req]
    if subprocess.call(base) != 0 and subprocess.call(base + ["--user"]) != 0:
        print("\nNão consegui instalar sozinho. Abra o terminal e rode:")
        print('    "' + sys.executable + '" -m pip install -r requirements.txt')
        sys.exit(1)
    importlib.invalidate_caches()
    import site
    try:  # caso tenha instalado com --user, deixa a pasta visível já nesta execução
        user_site = site.getusersitepackages()
        if user_site not in sys.path and os.path.isdir(user_site):
            sys.path.append(user_site)
    except Exception:
        pass


_instalar_o_que_falta()

import pymysql
import pymysql.cursors

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ler_config_mysql():
    """Lê os dados de acesso ao MySQL do config.ini (variáveis de ambiente têm prioridade)."""
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")
    sec = cfg["mysql"] if cfg.has_section("mysql") else {}
    return {
        "host": os.environ.get("MYSQL_HOST") or sec.get("host", "localhost"),
        "port": int(os.environ.get("MYSQL_PORT") or sec.get("porta", "3306")),
        "user": os.environ.get("MYSQL_USER") or sec.get("usuario", "root"),
        "password": os.environ.get("MYSQL_PASSWORD") if os.environ.get("MYSQL_PASSWORD") is not None
        else sec.get("senha", ""),
        "database": os.environ.get("MYSQL_DATABASE") or sec.get("banco", "delivery"),
    }


MYSQL = ler_config_mysql()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "troque-esta-chave-secreta"

# ------------------------------------------------------------------
# Configurações do restaurante
# ------------------------------------------------------------------
RESTAURANTE = {
    "nome": "Sabor de Casa",
    "slogan": "Comida caseira, quentinha, na sua porta.",
    "taxa_entrega": 6.00,
    "pedido_minimo": 20.00,
    "telefone": "(11) 99999-0000",
}
ADMIN_EMAIL = "admin@delivery.com"
ADMIN_SENHA = "admin123"

STATUS_FLUXO = ["recebido", "preparo", "a_caminho", "entregue"]
STATUS_LABEL = {
    "recebido": "Pedido recebido",
    "preparo": "Em preparo",
    "a_caminho": "Saiu para entrega",
    "entregue": "Entregue",
    "cancelado": "Cancelado",
}
PROXIMO_STATUS = {"recebido": "preparo", "preparo": "a_caminho", "a_caminho": "entregue"}
STATUS_ATIVOS = ("recebido", "preparo", "a_caminho")
PAGAMENTOS = {"pix": "Pix", "cartao": "Cartão na entrega", "dinheiro": "Dinheiro"}

CARDAPIO_INICIAL = [
    ("Lanches", [
        ("X-Burger", "Pão brioche, hambúrguer artesanal 150g, queijo e molho da casa.", 24.90, "🍔"),
        ("X-Bacon", "Hambúrguer 150g, bacon crocante, cheddar e cebola caramelizada.", 28.90, "🥓"),
        ("X-Salada", "Hambúrguer 150g, queijo, alface, tomate e maionese verde.", 26.90, "🥬"),
    ]),
    ("Pratos", [
        ("Prato feito", "Arroz, feijão, bife acebolado, salada e batata frita.", 32.90, "🍛"),
        ("Frango grelhado", "Peito de frango, arroz integral e legumes salteados.", 29.90, "🍗"),
        ("Feijoada", "Feijoada completa com arroz, couve, farofa e laranja.", 39.90, "🍲"),
    ]),
    ("Porções", [
        ("Batata frita", "400g de batata crocante com sal e alecrim.", 19.90, "🍟"),
        ("Pastéis (6 un.)", "Mix de carne, queijo e frango.", 22.90, "🥟"),
    ]),
    ("Bebidas", [
        ("Refrigerante lata", "350ml — Coca, Guaraná ou Soda.", 6.00, "🥤"),
        ("Suco natural", "500ml — laranja, limão ou maracujá.", 9.90, "🧃"),
        ("Água mineral", "500ml, com ou sem gás.", 4.00, "💧"),
    ]),
    ("Sobremesas", [
        ("Pudim", "Fatia generosa de pudim de leite condensado.", 10.90, "🍮"),
        ("Brownie", "Brownie de chocolate com calda quente.", 12.90, "🍫"),
    ]),
]

# Tabelas do banco MySQL (criadas automaticamente na primeira execução).
TABELAS = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nome VARCHAR(80) NOT NULL,
        email VARCHAR(120) NOT NULL UNIQUE,
        telefone VARCHAR(30),
        endereco VARCHAR(300),
        senha_hash VARCHAR(255) NOT NULL,
        is_admin TINYINT(1) NOT NULL DEFAULT 0,
        criado_em DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS categorias (
        id INT AUTO_INCREMENT PRIMARY KEY,
        nome VARCHAR(60) NOT NULL UNIQUE,
        ordem INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS produtos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        categoria_id INT NOT NULL,
        nome VARCHAR(80) NOT NULL,
        descricao VARCHAR(300),
        preco DECIMAL(10,2) NOT NULL,
        emoji VARCHAR(16) DEFAULT '🍽️',
        disponivel TINYINT(1) NOT NULL DEFAULT 1,
        FOREIGN KEY (categoria_id) REFERENCES categorias(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS pedidos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        endereco VARCHAR(300) NOT NULL,
        pagamento VARCHAR(20) NOT NULL,
        troco DECIMAL(10,2),
        observacao VARCHAR(300),
        subtotal DECIMAL(10,2) NOT NULL,
        taxa DECIMAL(10,2) NOT NULL,
        total DECIMAL(10,2) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'recebido',
        criado_em DATETIME NOT NULL,
        atualizado_em DATETIME NOT NULL,
        INDEX idx_pedidos_status (status),
        FOREIGN KEY (user_id) REFERENCES users(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS itens_pedido (
        id INT AUTO_INCREMENT PRIMARY KEY,
        pedido_id INT NOT NULL,
        produto_id INT,
        nome VARCHAR(80) NOT NULL,
        preco DECIMAL(10,2) NOT NULL,
        quantidade INT NOT NULL,
        FOREIGN KEY (pedido_id) REFERENCES pedidos(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS historico_status (
        id INT AUTO_INCREMENT PRIMARY KEY,
        pedido_id INT NOT NULL,
        status VARCHAR(20) NOT NULL,
        criado_em DATETIME NOT NULL,
        FOREIGN KEY (pedido_id) REFERENCES pedidos(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
]


# ------------------------------------------------------------------
# Banco de dados (MySQL — configure o acesso no arquivo config.ini)
# ------------------------------------------------------------------
def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar(com_banco=True):
    opcoes = dict(MYSQL)
    if not com_banco:
        opcoes.pop("database")
    return pymysql.connect(**opcoes, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False)


class Banco:
    """Conexão simples: db.execute(sql, valores) devolve o cursor (fetchone/fetchall/lastrowid)."""

    def __init__(self, conexao):
        self.conexao = conexao

    def execute(self, sql, valores=()):
        cur = self.conexao.cursor()
        cur.execute(sql, tuple(valores))
        return cur

    def commit(self):
        self.conexao.commit()

    def close(self):
        self.conexao.close()


def get_db():
    if "db" not in g:
        g.db = Banco(conectar())
    return g.db


@app.teardown_appcontext
def fechar_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    nome_banco = MYSQL["database"]
    if not nome_banco.replace("_", "").isalnum():
        raise SystemExit("O nome do banco no config.ini só pode ter letras, números e _.")
    servidor = conectar(com_banco=False)
    with servidor.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{nome_banco}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    servidor.commit()
    servidor.close()

    db = Banco(conectar())
    for tabela in TABELAS:
        db.execute(tabela)
    if not db.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():
        db.execute(
            "INSERT INTO users (nome, email, telefone, senha_hash, is_admin, criado_em) "
            "VALUES (%s, %s, %s, %s, 1, %s)",
            ("Dono do restaurante", ADMIN_EMAIL, "", generate_password_hash(ADMIN_SENHA), agora()),
        )
    if not db.execute("SELECT 1 FROM categorias").fetchone():
        for ordem, (categoria, itens) in enumerate(CARDAPIO_INICIAL):
            cur = db.execute("INSERT INTO categorias (nome, ordem) VALUES (%s, %s)", (categoria, ordem))
            for nome, descricao, preco, emoji in itens:
                db.execute(
                    "INSERT INTO produtos (categoria_id, nome, descricao, preco, emoji) VALUES (%s, %s, %s, %s, %s)",
                    (cur.lastrowid, nome, descricao, preco, emoji),
                )
    db.commit()
    db.close()


def iniciar_banco():
    try:
        init_db()
    except pymysql.err.OperationalError as erro:
        codigo = erro.args[0] if erro.args else None
        print("\n  NÃO FOI POSSÍVEL CONECTAR AO MYSQL")
        if codigo == 1045:
            print("  Usuário ou senha do MySQL incorretos. Confira o arquivo config.ini.")
        elif codigo in (2003, 2002):
            print(f"  O MySQL não respondeu em {MYSQL['host']}:{MYSQL['port']}.")
            print("  Verifique se o MySQL está instalado e ligado (no XAMPP, clique em Start no MySQL).")
        else:
            print(f"  Detalhe: {erro}")
        print("  Veja o passo a passo no arquivo LEIA-ME.txt.\n")
        sys.exit(1)


iniciar_banco()


# ------------------------------------------------------------------
# Formatação, segurança e dados comuns nas páginas
# ------------------------------------------------------------------
@app.template_filter("brl")
def brl(valor):
    texto = f"{float(valor or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


@app.template_filter("datahora")
def datahora(valor):
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m às %H:%M")
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m às %H:%M")
    except (TypeError, ValueError):
        return valor


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def verificar_csrf():
    if request.method == "POST":
        enviado = request.form.get("_csrf")
        if not enviado or enviado != session.get("_csrf"):
            abort(400, "Sessão expirada. Volte e tente novamente.")


@app.before_request
def carregar_usuario():
    uid = session.get("user_id")
    g.user = get_db().execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone() if uid else None


@app.context_processor
def dados_globais():
    carrinho = session.get("carrinho", {})
    return {
        "R": RESTAURANTE,
        "STATUS_LABEL": STATUS_LABEL,
        "PAGAMENTOS": PAGAMENTOS,
        "carrinho_qtd": sum(carrinho.values()),
        "csrf": lambda: Markup(f'<input type="hidden" name="_csrf" value="{escape(csrf_token())}">'),
    }


def login_obrigatorio(view):
    @wraps(view)
    def interno(*args, **kwargs):
        if g.user is None:
            flash("Entre na sua conta para continuar.", "info")
            return redirect(url_for("entrar", next=request.path))
        return view(*args, **kwargs)
    return interno


def admin_obrigatorio(view):
    @wraps(view)
    def interno(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("entrar", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return interno


def destino_seguro(url):
    return url if url and url.startswith("/") and not url.startswith("//") else None


# ------------------------------------------------------------------
# Carrinho (guardado na sessão do navegador)
# ------------------------------------------------------------------
def itens_do_carrinho():
    carrinho = session.get("carrinho", {})
    if not carrinho:
        return [], 0.0
    ids = [int(k) for k in carrinho]
    marcadores = ",".join(["%s"] * len(ids))
    produtos = get_db().execute(
        f"SELECT * FROM produtos WHERE id IN ({marcadores}) AND disponivel = 1", ids
    ).fetchall()
    itens, subtotal = [], 0.0
    validos = {}
    for p in produtos:
        qtd = carrinho[str(p["id"])]
        validos[str(p["id"])] = qtd
        total = round(float(p["preco"]) * qtd, 2)
        itens.append({"produto": p, "qtd": qtd, "total": total})
        subtotal += total
    if validos != carrinho:  # remove itens que saíram do cardápio
        session["carrinho"] = validos
    itens.sort(key=lambda i: i["produto"]["nome"])
    return itens, round(subtotal, 2)


# ------------------------------------------------------------------
# Cliente: cardápio, carrinho, pedido
# ------------------------------------------------------------------
@app.route("/")
def cardapio():
    db = get_db()
    categorias = db.execute("SELECT * FROM categorias ORDER BY ordem, nome").fetchall()
    produtos = db.execute("SELECT * FROM produtos WHERE disponivel = 1 ORDER BY nome").fetchall()
    por_categoria = {c["id"]: [] for c in categorias}
    for p in produtos:
        por_categoria.setdefault(p["categoria_id"], []).append(p)
    secoes = [(c, por_categoria[c["id"]]) for c in categorias if por_categoria[c["id"]]]
    return render_template("cardapio.html", secoes=secoes, carrinho=session.get("carrinho", {}))


@app.post("/carrinho/adicionar/<int:pid>")
def carrinho_adicionar(pid):
    produto = get_db().execute("SELECT * FROM produtos WHERE id = %s AND disponivel = 1", (pid,)).fetchone()
    if not produto:
        abort(404)
    carrinho = session.get("carrinho", {})
    carrinho[str(pid)] = min(carrinho.get(str(pid), 0) + 1, 50)
    session["carrinho"] = carrinho
    flash(f"{produto['nome']} foi para o carrinho.", "ok")
    return redirect(url_for("cardapio") + f"#p{pid}")


@app.post("/carrinho/alterar/<int:pid>")
def carrinho_alterar(pid):
    carrinho = session.get("carrinho", {})
    chave, acao = str(pid), request.form.get("acao")
    if chave in carrinho:
        if acao == "mais":
            carrinho[chave] = min(carrinho[chave] + 1, 50)
        elif acao == "menos":
            carrinho[chave] -= 1
        elif acao == "remover":
            carrinho[chave] = 0
        if carrinho[chave] <= 0:
            del carrinho[chave]
    session["carrinho"] = carrinho
    return redirect(url_for("ver_carrinho"))


@app.route("/carrinho")
def ver_carrinho():
    itens, subtotal = itens_do_carrinho()
    taxa = RESTAURANTE["taxa_entrega"] if itens else 0.0
    falta = max(0.0, RESTAURANTE["pedido_minimo"] - subtotal)
    return render_template("carrinho.html", itens=itens, subtotal=subtotal, taxa=taxa,
                           total=subtotal + taxa, falta=falta)


@app.route("/finalizar", methods=["GET", "POST"])
@login_obrigatorio
def finalizar():
    itens, subtotal = itens_do_carrinho()
    if not itens:
        flash("Seu carrinho está vazio.", "erro")
        return redirect(url_for("cardapio"))
    if subtotal < RESTAURANTE["pedido_minimo"]:
        flash(f"O pedido mínimo é {brl(RESTAURANTE['pedido_minimo'])}.", "erro")
        return redirect(url_for("ver_carrinho"))
    taxa = RESTAURANTE["taxa_entrega"]
    total = round(subtotal + taxa, 2)

    if request.method == "POST":
        endereco = request.form.get("endereco", "").strip()[:300]
        pagamento = request.form.get("pagamento", "")
        troco_txt = request.form.get("troco", "").strip().replace("R$", "").replace(",", ".").strip()
        observacao = request.form.get("observacao", "").strip()[:300]
        erros, troco = [], None
        if len(endereco) < 8:
            erros.append("Informe o endereço completo (rua, número e bairro).")
        if pagamento not in PAGAMENTOS:
            erros.append("Escolha a forma de pagamento.")
        if pagamento == "dinheiro" and troco_txt:
            try:
                troco = float(troco_txt)
                if troco < total:
                    erros.append("O troco precisa ser para um valor maior que o total.")
            except ValueError:
                erros.append("Valor do troco inválido.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("finalizar.html", itens=itens, subtotal=subtotal, taxa=taxa,
                                   total=total, form=request.form)

        db = get_db()
        momento = agora()
        cur = db.execute(
            "INSERT INTO pedidos (user_id, endereco, pagamento, troco, observacao, subtotal, taxa, total, "
            "status, criado_em, atualizado_em) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'recebido', %s, %s)",
            (g.user["id"], endereco, pagamento, troco, observacao, subtotal, taxa, total, momento, momento),
        )
        pedido_id = cur.lastrowid
        for item in itens:
            p = item["produto"]
            db.execute(
                "INSERT INTO itens_pedido (pedido_id, produto_id, nome, preco, quantidade) VALUES (%s, %s, %s, %s, %s)",
                (pedido_id, p["id"], p["nome"], p["preco"], item["qtd"]),
            )
        db.execute("INSERT INTO historico_status (pedido_id, status, criado_em) VALUES (%s, 'recebido', %s)",
                   (pedido_id, momento))
        db.execute("UPDATE users SET endereco = %s WHERE id = %s", (endereco, g.user["id"]))
        db.commit()
        session.pop("carrinho", None)
        flash("Pedido enviado! Acompanhe o andamento abaixo.", "ok")
        return redirect(url_for("acompanhar", pid=pedido_id))

    return render_template("finalizar.html", itens=itens, subtotal=subtotal, taxa=taxa, total=total,
                           form={"endereco": g.user["endereco"] or "", "pagamento": "pix"})


@app.route("/meus-pedidos")
@login_obrigatorio
def meus_pedidos():
    pedidos = get_db().execute(
        "SELECT p.*, (SELECT SUM(quantidade) FROM itens_pedido WHERE pedido_id = p.id) AS qtd_itens "
        "FROM pedidos p WHERE user_id = %s ORDER BY id DESC", (g.user["id"],)
    ).fetchall()
    return render_template("pedidos.html", pedidos=pedidos)


def buscar_pedido_permitido(pid):
    pedido = get_db().execute("SELECT * FROM pedidos WHERE id = %s", (pid,)).fetchone()
    if not pedido or (pedido["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    return pedido


@app.route("/pedido/<int:pid>")
@login_obrigatorio
def acompanhar(pid):
    pedido = buscar_pedido_permitido(pid)
    db = get_db()
    itens = db.execute("SELECT * FROM itens_pedido WHERE pedido_id = %s", (pid,)).fetchall()
    historico = db.execute("SELECT * FROM historico_status WHERE pedido_id = %s ORDER BY id", (pid,)).fetchall()
    momentos = {h["status"]: h["criado_em"] for h in historico}
    etapa_atual = STATUS_FLUXO.index(pedido["status"]) if pedido["status"] in STATUS_FLUXO else -1
    return render_template("pedido.html", pedido=pedido, itens=itens, etapas=STATUS_FLUXO,
                           etapa_atual=etapa_atual, momentos=momentos)


@app.post("/pedido/<int:pid>/cancelar")
@login_obrigatorio
def cancelar_pedido(pid):
    pedido = buscar_pedido_permitido(pid)
    if pedido["status"] != "recebido":
        flash("Este pedido já está em preparo e não pode mais ser cancelado pelo app. Ligue para o restaurante.", "erro")
    else:
        mudar_status(pid, "cancelado")
        flash("Pedido cancelado.", "info")
    return redirect(url_for("acompanhar", pid=pid))


def mudar_status(pid, novo):
    db = get_db()
    momento = agora()
    db.execute("UPDATE pedidos SET status = %s, atualizado_em = %s WHERE id = %s", (novo, momento, pid))
    db.execute("INSERT INTO historico_status (pedido_id, status, criado_em) VALUES (%s, %s, %s)", (pid, novo, momento))
    db.commit()


# ------------------------------------------------------------------
# Conta do cliente
# ------------------------------------------------------------------
def iniciar_sessao(user_id):
    carrinho = session.get("carrinho", {})
    session.clear()
    session["user_id"] = user_id
    session["carrinho"] = carrinho


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()[:80]
        email = request.form.get("email", "").strip().lower()[:120]
        telefone = request.form.get("telefone", "").strip()[:30]
        senha = request.form.get("senha", "")
        erros = []
        if len(nome) < 2:
            erros.append("Informe seu nome.")
        if "@" not in email or "." not in email.split("@")[-1]:
            erros.append("E-mail inválido.")
        if len(telefone) < 8:
            erros.append("Informe um telefone para contato.")
        if len(senha) < 6:
            erros.append("A senha precisa ter pelo menos 6 caracteres.")
        if senha != request.form.get("senha2", ""):
            erros.append("As senhas não conferem.")
        db = get_db()
        if not erros and db.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            erros.append("Já existe uma conta com este e-mail.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("cadastro.html", form=request.form)
        cur = db.execute(
            "INSERT INTO users (nome, email, telefone, senha_hash, criado_em) VALUES (%s, %s, %s, %s, %s)",
            (nome, email, telefone, generate_password_hash(senha), agora()),
        )
        db.commit()
        iniciar_sessao(cur.lastrowid)
        flash(f"Bem-vindo(a), {nome.split()[0]}!", "ok")
        return redirect(destino_seguro(request.args.get("next")) or url_for("cardapio"))
    return render_template("cadastro.html", form={})


@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        user = get_db().execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if not user or not check_password_hash(user["senha_hash"], senha):
            flash("E-mail ou senha incorretos.", "erro")
            return render_template("entrar.html", form=request.form)
        iniciar_sessao(user["id"])
        destino = destino_seguro(request.args.get("next"))
        if not destino:
            destino = url_for("admin_pedidos") if user["is_admin"] else url_for("cardapio")
        return redirect(destino)
    return render_template("entrar.html", form={})


@app.post("/sair")
def sair():
    session.clear()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for("cardapio"))


# ------------------------------------------------------------------
# Painel do dono
# ------------------------------------------------------------------
@app.route("/admin")
@admin_obrigatorio
def admin_pedidos():
    db = get_db()
    filtro = request.args.get("status", "ativos")
    base = ("SELECT p.*, u.nome AS cliente, u.telefone AS telefone FROM pedidos p "
            "JOIN users u ON u.id = p.user_id")
    if filtro == "ativos":
        pedidos = db.execute(base + " WHERE p.status IN (%s, %s, %s) ORDER BY p.id", STATUS_ATIVOS).fetchall()
    elif filtro in STATUS_LABEL:
        pedidos = db.execute(base + " WHERE p.status = %s ORDER BY p.id DESC LIMIT 100", (filtro,)).fetchall()
    else:
        filtro = "todos"
        pedidos = db.execute(base + " ORDER BY p.id DESC LIMIT 100").fetchall()

    itens_por_pedido = {}
    if pedidos:
        ids = [p["id"] for p in pedidos]
        for item in db.execute(
            f"SELECT * FROM itens_pedido WHERE pedido_id IN ({','.join(['%s'] * len(ids))})", ids
        ):
            itens_por_pedido.setdefault(item["pedido_id"], []).append(item)

    contagem = {s: 0 for s in STATUS_LABEL}
    for linha in db.execute("SELECT status, COUNT(*) AS n FROM pedidos GROUP BY status"):
        contagem[linha["status"]] = linha["n"]
    hoje = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS valor FROM pedidos "
        "WHERE DATE(criado_em) = %s AND status != 'cancelado'", (datetime.now().strftime("%Y-%m-%d"),)
    ).fetchone()
    return render_template("admin/pedidos.html", pedidos=pedidos, itens_por_pedido=itens_por_pedido,
                           filtro=filtro, contagem=contagem, hoje=hoje, proximo=PROXIMO_STATUS,
                           ativos=STATUS_ATIVOS)


@app.post("/admin/pedido/<int:pid>/status")
@admin_obrigatorio
def admin_mudar_status(pid):
    novo = request.form.get("status")
    pedido = get_db().execute("SELECT * FROM pedidos WHERE id = %s", (pid,)).fetchone()
    if not pedido or novo not in STATUS_LABEL:
        abort(400)
    if pedido["status"] != novo:
        mudar_status(pid, novo)
        flash(f"Pedido #{pid}: {STATUS_LABEL[novo]}.", "ok")
    filtro = request.form.get("filtro", "ativos")
    return redirect(url_for("admin_pedidos", status=filtro))


@app.route("/admin/cardapio")
@admin_obrigatorio
def admin_cardapio():
    db = get_db()
    categorias = db.execute("SELECT * FROM categorias ORDER BY ordem, nome").fetchall()
    produtos = db.execute("SELECT * FROM produtos ORDER BY nome").fetchall()
    por_categoria = {c["id"]: [] for c in categorias}
    for p in produtos:
        por_categoria.setdefault(p["categoria_id"], []).append(p)
    return render_template("admin/cardapio.html", categorias=categorias, por_categoria=por_categoria)


@app.route("/admin/produto/novo", methods=["GET", "POST"])
@app.route("/admin/produto/<int:pid>", methods=["GET", "POST"])
@admin_obrigatorio
def admin_produto(pid=None):
    db = get_db()
    produto = None
    if pid is not None:
        produto = db.execute("SELECT * FROM produtos WHERE id = %s", (pid,)).fetchone()
        if not produto:
            abort(404)
    categorias = db.execute("SELECT * FROM categorias ORDER BY ordem, nome").fetchall()

    if request.method == "POST":
        f = request.form
        nome = f.get("nome", "").strip()[:80]
        descricao = f.get("descricao", "").strip()[:300]
        emoji = f.get("emoji", "").strip()[:4] or "🍽️"
        nova_categoria = f.get("nova_categoria", "").strip()[:40]
        disponivel = 1 if f.get("disponivel") else 0
        erros = []
        try:
            preco = round(float(f.get("preco", "").replace("R$", "").replace(",", ".").strip()), 2)
            if preco <= 0:
                raise ValueError
        except ValueError:
            preco = None
            erros.append("Preço inválido.")
        if len(nome) < 2:
            erros.append("Informe o nome do produto.")
        categoria_id = None
        if nova_categoria:
            existente = db.execute("SELECT id FROM categorias WHERE nome = %s", (nova_categoria,)).fetchone()
            if existente:
                categoria_id = existente["id"]
            elif not erros:
                ordem = db.execute("SELECT COALESCE(MAX(ordem), 0) + 1 AS proxima FROM categorias").fetchone()["proxima"]
                categoria_id = db.execute("INSERT INTO categorias (nome, ordem) VALUES (%s, %s)",
                                          (nova_categoria, ordem)).lastrowid
        else:
            try:
                categoria_id = int(f.get("categoria_id", ""))
            except ValueError:
                erros.append("Escolha uma categoria.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("admin/produto.html", produto=produto, categorias=categorias, form=f)
        if produto:
            db.execute("UPDATE produtos SET categoria_id=%s, nome=%s, descricao=%s, preco=%s, emoji=%s, disponivel=%s "
                       "WHERE id=%s", (categoria_id, nome, descricao, preco, emoji, disponivel, pid))
            flash("Produto atualizado.", "ok")
        else:
            db.execute("INSERT INTO produtos (categoria_id, nome, descricao, preco, emoji, disponivel) "
                       "VALUES (%s, %s, %s, %s, %s, %s)", (categoria_id, nome, descricao, preco, emoji, disponivel))
            flash("Produto adicionado ao cardápio.", "ok")
        db.commit()
        return redirect(url_for("admin_cardapio"))

    form = dict(produto) if produto else {"disponivel": 1, "emoji": "🍽️"}
    if produto:
        form["preco"] = f"{float(produto['preco']):.2f}".replace(".", ",")
    return render_template("admin/produto.html", produto=produto, categorias=categorias, form=form)


@app.post("/admin/produto/<int:pid>/alternar")
@admin_obrigatorio
def admin_alternar_produto(pid):
    db = get_db()
    db.execute("UPDATE produtos SET disponivel = 1 - disponivel WHERE id = %s", (pid,))
    db.commit()
    return redirect(url_for("admin_cardapio"))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def pagina_erro(erro):
    mensagens = {400: "Algo deu errado com o envio. Volte e tente de novo.",
                 403: "Esta área é só para o dono do restaurante.",
                 404: "Página não encontrada."}
    return render_template("erro.html", codigo=erro.code, mensagem=mensagens.get(erro.code)), erro.code


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    print(f"\n  {RESTAURANTE['nome']} rodando em http://localhost:{porta}")
    print(f"  Painel do dono: {ADMIN_EMAIL} / {ADMIN_SENHA}\n")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{porta}")).start()
    app.run(host="127.0.0.1", port=porta, debug=False)
