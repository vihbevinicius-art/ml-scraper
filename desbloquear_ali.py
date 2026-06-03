"""Abre o Chrome com o profile persistente do scraper VISÍVEL na tela,
navegado pro AliExpress. Usuário arrasta o slider do captcha manualmente.
Quando passa no captcha (ou simplesmente navega na home), o cookie de
'usuário verificado' fica salvo no profile e o scraper volta a funcionar
em modo headless/offscreen pelas próximas horas/dias.

Esse script é disparado pelo endpoint /aliexpress/desbloquear do main.py
via subprocess — fica vivo até o usuário fechar a janela manualmente.

Log de execução em /tmp/desbloquear_ali.log.
"""
import pathlib
import subprocess
import sys
import time
import traceback

LOG = pathlib.Path("/tmp/desbloquear_ali.log")
PROFILE = pathlib.Path.home() / ".ml-mensagens-chrome-profile"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, app: {} };
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }]
});
Object.defineProperty(navigator, 'languages', {
  get: () => ['pt-BR','pt','en-US','en']
});
"""


def log(msg: str) -> None:
    """Append ao log + stderr."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    try:
        with LOG.open("a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


def trazer_chrome_pra_frente() -> None:
    """No macOS, traz a janela do Chrome pra frente via AppleScript.
    Sem isso, o Chrome pode abrir em background e o usuário não percebe."""
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            timeout=3, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"osascript falhou: {e}")


def main() -> int:
    log("=" * 60)
    log("Iniciando desbloqueio do AliExpress")
    PROFILE.mkdir(exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("ERRO: Playwright não instalado")
        return 1

    try:
        with sync_playwright() as p:
            log("Lançando Chrome com profile persistente (visível)")
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE),
                channel="chrome",
                headless=False,  # VISÍVEL — usuário precisa interagir
                user_agent=UA,
                locale="pt-BR",
                viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900",
                    "--window-position=100,100",  # canto sup-esq visível
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            ctx.add_init_script(STEALTH)
            page = ctx.new_page()
            log(f"Chrome aberto (PID: {p.chromium.executable_path})")

            try:
                page.goto(
                    "https://pt.aliexpress.com/",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                log(f"Página carregada: {page.url[:120]}")
            except Exception as e:
                log(f"Aviso ao carregar AliExpress: {e}")

            # Pequena pausa pra Chrome terminar de abrir, então traz pro foco
            time.sleep(0.5)
            trazer_chrome_pra_frente()
            log("Chrome trazido pro foco — aguardando usuário fechar a janela")

            # Aguarda até o usuário fechar a janela do Chrome (10min máx)
            try:
                page.wait_for_event("close", timeout=10 * 60 * 1000)
                log("Página fechada pelo usuário")
            except Exception as e:
                log(f"Timeout ou erro aguardando close: {e}")

            try:
                ctx.close()
                log("Contexto fechado")
            except Exception:
                pass

        log("Concluído com sucesso")
        return 0
    except Exception as e:
        log(f"ERRO: {e}\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
