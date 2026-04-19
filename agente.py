import asyncio
import json
import os
import logging
import re
import subprocess # <-- Csubprocess para o CLI
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain.tools import tool 

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools

def resolver_vault_path() -> Path:
    caminho_configurado = os.getenv("OBSIDIAN_VAULT_PATH")
    if caminho_configurado:
        return Path(caminho_configurado)

    candidatos = [
        Path.home() / "Documents" / "SecondBrain",
        Path(r"C:\Users\gabri\Documents\SecondBrain"),
        Path(r"C:\Users\gabriel\Documents\SecondBrain"),
    ]

    for candidato in candidatos:
        if candidato.exists():
            return candidato

    return candidatos[0]


VAULT_PATH = resolver_vault_path()
MEMORIA_LONGA_PATH = VAULT_PATH / "JARVIS" / "memoria_longa.md"
MEMORIA_LONGA_RELATIVA = "JARVIS/memoria_longa.md"

# 1. Carrega as chaves e limpa o terminal
load_dotenv()
logging.getLogger("langchain_core.utils.json_schema").setLevel(logging.ERROR)
logging.getLogger("langchain_core").setLevel(logging.ERROR)
logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
logging.getLogger("langchain_google_genai._function_utils").setLevel(logging.ERROR)

# 2. Inicializa o Motor
llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.6
)

# ==========================================
# 3. A FERRAMENTA CUSTOMIZADA (CLI)
# ==========================================
@tool
def executar_powershell(comando: str) -> str:
    """Executa comandos nativos no PowerShell do Windows 11 e retorna a saída."""
    try:
        resultado = subprocess.run(
            ["powershell", "-Command", comando],
            capture_output=True, text=True, timeout=30
        )
        if resultado.returncode == 0:
            return resultado.stdout if resultado.stdout else "Comando executado com sucesso."
        else:
            return f"Erro do PowerShell:\n{resultado.stderr}"
    except Exception as e:
        return f"Erro no sistema: {str(e)}"


def carregar_memoria_longa(limitador_caracteres: int = 4000) -> str:
    if not MEMORIA_LONGA_PATH.exists():
        return ""

    conteudo = MEMORIA_LONGA_PATH.read_text(encoding="utf-8")
    if len(conteudo) <= limitador_caracteres:
        return conteudo.strip()

    return conteudo[-limitador_caracteres:].strip()


def salvar_memoria_longa(pergunta: str, resposta: str) -> None:
    MEMORIA_LONGA_PATH.parent.mkdir(parents=True, exist_ok=True)
    carimbo_tempo = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entrada = (
        f"\n## {carimbo_tempo}\n"
        f"- Usuario: {pergunta.strip()}\n"
        f"- Jarvis: {resposta.strip()}\n"
    )

    with MEMORIA_LONGA_PATH.open("a", encoding="utf-8") as arquivo:
        arquivo.write(entrada)


def converter_conteudo_para_texto(conteudo) -> str:
    if isinstance(conteudo, str):
        return conteudo

    if isinstance(conteudo, list):
        partes: list[str] = []
        for item in conteudo:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict):
                if "text" in item and isinstance(item["text"], str):
                    partes.append(item["text"])
                else:
                    partes.append(json.dumps(item, ensure_ascii=False))
            else:
                partes.append(str(item))

        return "\n".join(parte.strip() for parte in partes if parte.strip())

    return str(conteudo)


def extrair_resposta_final(mensagens: list) -> str:
    for mensagem in reversed(mensagens):
        if getattr(mensagem, "type", None) == "ai" or mensagem.__class__.__name__ == "AIMessage":
            return converter_conteudo_para_texto(mensagem.content)

    return converter_conteudo_para_texto(mensagens[-1].content) if mensagens else ""


def responder_localmente_por_memoria(pergunta: str, memoria_longa: str) -> str:
    pergunta_normalizada = pergunta.lower().strip()
    memoria_normalizada = memoria_longa.lower()

    if re.search(r"(cor do meu cabelo|como.*cabelo|meu cabelo)", pergunta_normalizada):
        if "cabelo loiro" in memoria_normalizada or "tenho cabelo loiro" in memoria_normalizada:
            return "Você tem cabelo loiro."
        if "cabelo castanho" in memoria_normalizada:
            return "Você tem cabelo castanho."
        if "cabelo preto" in memoria_normalizada:
            return "Você tem cabelo preto."

    if re.search(r"\b(qual a minha idade|quantos anos eu tenho|minha idade)\b", pergunta_normalizada):
        match_idade = re.search(r"(\d{1,3})\s+anos", memoria_normalizada)
        if match_idade:
            return f"Você tem {match_idade.group(1)} anos."

    if memoria_longa:
        trecho = memoria_longa[-800:].strip()
        return (
            "O modelo ficou indisponível por limite de cota, mas encontrei este contexto recente na memória:\n"
            f"{trecho}"
        )

    return "O modelo ficou indisponível por limite de cota e não encontrei memória suficiente para responder agora."

# ==========================================
# 4. ORQUESTRAÇÃO PRINCIPAL
# ==========================================
async def executar_jarvis():
    print("Iniciando sistemas e conectando ao Servidor MCP do Obsidian...")

    if not VAULT_PATH.exists():
        print(f"O.R.C.A.: o vault configurado não existe em {VAULT_PATH}")
        print("O.R.C.A.: defina a variável de ambiente OBSIDIAN_VAULT_PATH para apontar para o vault correto.")
        return
    
    server_params = StdioServerParameters(
        command="npx.cmd", 
        args=[
            "-y", 
            "@modelcontextprotocol/server-filesystem", 
            str(VAULT_PATH)
        ]
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Pegamos as ferramentas do Obsidian
            ferramentas_mcp = await load_mcp_tools(session)
            
            # Unimos o CLI com o MCP
            todas_as_ferramentas = ferramentas_mcp + [executar_powershell]

            identificador_sessao = os.getenv(
                "JARVIS_THREAD_ID",
                f"sessao_jarvis_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            )
            
            async with AsyncSqliteSaver.from_conn_string("memoria_jarvis.db") as memoria_sessao:
                configuracao_thread = {"configurable": {"thread_id": identificador_sessao}}
                
                # Passamos a lista combinada para o agente
                agente = create_react_agent(
                    llm_gemini, 
                    tools=todas_as_ferramentas, 
                    checkpointer=memoria_sessao
                )
                
                print("="*50)
                print("SISTEMA O.R.C.A. [POWER-MODE ATIVADO]")
                print(f"Sessao ativa: {identificador_sessao}")
                print(f"Ferramentas Totais: {[t.name for t in todas_as_ferramentas]}")
                print("="*50)
                
                primeira_interacao = True
                
                while True:
                    comando = input("\nVocê: ")
                    
                    if comando.lower() in ['sair', 'exit', 'quit']:
                        print("O.R.C.A.: Encerrando conexões e salvando banco de dados.")
                        break
                    
                    mensagens_enviar = []
                    
                    if primeira_interacao:
                        mensagens_enviar.append(SystemMessage(content=(
                            "Você é o O.R.C.A., um amigo meu que consegue operar no Windows 11. "
                            "Você tem TRÊS capacidades principais: "
                            "1. Usar a ferramenta 'executar_powershell' para toda gestao de arquivos e diretorios locais (criar, editar, mover, listar) fora do vault. "
                            f"2. Usar ferramentas MCP apenas para operacoes dentro do vault do Obsidian {VAULT_PATH.name}. e organizar memória de longa duração. "
                            "3. Conversar e responder perguntas usando o modelo Gemini-2.5-flash de forma criativa e inteligente. "
                            "Regra obrigatoria: se o usuario pedir qualquer acao em pastas locais do computador que nao sejam o vault, use CLI (executar_powershell), nunca MCP. "
                            "Se uma tentativa com MCP falhar por path fora de diretorio permitido, tente novamente via CLI com caminho local correto. "
                            f"A memória longa é gerenciada automaticamente pelo aplicativo em {MEMORIA_LONGA_RELATIVA}; não crie nem escreva esse arquivo via MCP, a menos que o usuário peça explicitamente. "
                            f"Se precisar referenciar caminhos do vault, use apenas caminhos relativos dentro de {VAULT_PATH.name}. "
                            "Sempre escolha a ferramenta correta para o trabalho."
                        )))
                        primeira_interacao = False

                    memoria_longa = carregar_memoria_longa()
                    if memoria_longa:
                        mensagens_enviar.append(SystemMessage(content=(
                            "Memória de longo prazo disponível no vault do Obsidian. Use-a quando ajudar a responder e manter contexto:\n"
                            f"{memoria_longa}"
                        )))
                    
                    mensagens_enviar.append(("user", comando))
                        
                    try:
                        estado_final = await agente.ainvoke(
                            {"messages": mensagens_enviar},
                            config=configuracao_thread
                        )
                    except Exception as erro:
                        resposta_fallback = None
                        mensagem_erro = str(erro)
                        if "RESOURCE_EXHAUSTED" in mensagem_erro or "quota" in mensagem_erro.lower():
                            resposta_fallback = responder_localmente_por_memoria(comando, memoria_longa)

                        if (
                            resposta_fallback is None
                            and "path outside allowed directories" in mensagem_erro.lower()
                        ):
                            mensagens_retry = [
                                SystemMessage(content=(
                                    "A ultima tentativa falhou por restricao de caminho do MCP. "
                                    "Refaca a tarefa usando APENAS a ferramenta executar_powershell para operar no sistema de arquivos local fora do vault."
                                )),
                                ("user", comando),
                            ]
                            try:
                                estado_retry = await agente.ainvoke(
                                    {"messages": mensagens_retry},
                                    config=configuracao_thread
                                )
                                resposta_retry = extrair_resposta_final(estado_retry["messages"])
                                print(f"\nO.R.C.A.: {resposta_retry}")
                                salvar_memoria_longa(comando, resposta_retry)
                                continue
                            except Exception:
                                pass

                        if resposta_fallback is None:
                            print(f"\nO.R.C.A.: erro ao consultar MCP ou executar ferramentas: {erro}")
                            continue

                        print(f"\nO.R.C.A.: {resposta_fallback}")
                        salvar_memoria_longa(comando, resposta_fallback)
                        continue
                    
                    resposta_jarvis = extrair_resposta_final(estado_final["messages"])
                    print(f"\nO.R.C.A.: {resposta_jarvis}")
                    salvar_memoria_longa(comando, resposta_jarvis)

if __name__ == "__main__":
    asyncio.run(executar_jarvis())