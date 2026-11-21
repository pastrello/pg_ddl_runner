# PG DDL Runner no Windows — 0.5.1

O mesmo executor PostgreSQL da versão Linux agora dispõe de instalador e lançador para Windows 10/11 e Windows Server (ambiente Python 3.10+ x64). **Os checkpoints, hashes, identificadores e comandos SQL são os mesmos**, pois a execução ocorre no PostgreSQL; o sistema operacional do cliente não os modifica. Suporte Windows em fase de validação: testes offline disponíveis, ainda não homologado contra PostgreSQL a partir de um host Windows.

## Instalação sem administrador

1. Instale **Python 3.10 ou superior** para Windows, com `venv`, `pip` e acesso à internet (ou cache local das dependências). O instalador utiliza `py -3` ou `python` no PATH. Recomenda-se Python 3.12 x64.
2. Descompacte o ZIP em uma pasta de usuário local, por exemplo `C:\pg-ddl-runner`. Evite diretórios protegidos, pastas públicas/compartilhadas e caminhos UNC.
3. Abra PowerShell ou Prompt de Comando nessa pasta e execute:

```powershell
.\install-windows.cmd
.\pgddl.cmd --version
.\pgddl.cmd -h
```

O instalador cria `.venv\Scripts\python.exe` e instala `psycopg[binary]` apenas nesse ambiente virtual. Não instala serviço, não modifica PostgreSQL, não exige privilégios administrativos e não altera o PATH global. Para usar em outro computador, execute o instalador novamente nessa máquina; **não copie** o diretório `.venv` entre sistemas.

## Conexão ao PostgreSQL

No Windows normalmente se usa **TCP**, não o socket Unix nem autenticação `peer`. O usuário PostgreSQL precisa de autorização no `pg_hba.conf` e conectividade ao host por uma rede confiável ou VPN. Não publique a porta PostgreSQL indiscriminadamente na internet.

Configure a credencial pelo mecanismo oficial libpq: o arquivo `%APPDATA%\postgresql\pgpass.conf` (ou a variável `PGPASSFILE`), com as permissões restritas ao seu usuário. Evite fornecer senha como argumento de linha de comando ou registrar credenciais no repositório.

```powershell
.\pgddl.cmd plan "C:\dados\atualizacao.ddl"
.\pgddl.cmd dry-run "C:\dados\atualizacao.ddl" -H 10.10.10.20 -p 15777 -d erp -U postgres
.\pgddl.cmd status "C:\dados\atualizacao.ddl" -H 10.10.10.20 -p 15777 -d erp -U postgres
.\pgddl.cmd report "C:\dados\atualizacao.ddl" -H 10.10.10.20 -p 15777 -d erp -U postgres --validate
```

`plan`, `-h` e `--version` são operações offline. `dry-run`, `status`, `report` e `validate` consultam o banco, mas não aplicam o DDL do fornecedor. `apply` **modifica o banco**; `ack` modifica o histórico; valide antes a instância alvo. Para atualizar com segurança em ambiente de teste:

```powershell
.\pgddl.cmd apply "C:\dados\atualizacao.ddl" -H 10.10.10.20 -p 15777 -d erp -U postgres --max-steps 1
```

Sem `--yes`, `apply` pede confirmação do nome do banco quando executado interativamente. A reconstrução de views comuns permanece **desabilitada por padrão** (`--dependency-policy stop`). Nenhuma materialized view é reconstruída automaticamente. Não utilize `--dependency-policy rebuild-views` sem revisar e autorizar o comportamento no banco específico.

## Diretórios de trabalho

O novo padrão no Windows é `%LOCALAPPDATA%\PGDDL\`:

```text
%LOCALAPPDATA%\PGDDL\
  logs\
  reports\
  views\
```

Para personalizar o diretório **nesta sessão** do PowerShell:

```powershell
$env:PGDDL_STATE_HOME = 'D:\pgddl-state'
```

Opções como `--report-dir`, `--backup-dir` e `--log` continuam disponíveis e prevalecem quando fornecidas. No Linux, permanecem os caminhos anteriores `~/.local/state/pgddl/` ou `XDG_STATE_HOME/pgddl/`.

Os nomes de arquivos datados são ajustados automaticamente: no Windows, `21-11-2026_18-59` (sem `:`); no Linux, o formato anterior `21-11-2026_18:59` é preservado. **O conteúdo dos backups de views, os hashes do DDL e os checkpoints não são reformatados.**

No Windows, as permissões POSIX `0600`/`0700` não garantem proteção por ACL NTFS. Use pastas dentro do seu perfil, com ACLs restritas; evite compartilhar ou publicar relatórios, logs ou backups, pois podem conter SQL, nomes internos e metadados.

## Testes locais

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p 'test_*.py' -v
.\pgddl.cmd -h
.\pgddl.cmd apply -h
```

Não execute `integration_lab.py` ou `integration_views.py` em uma base real: ambos alteram o banco e são apropriados apenas para um banco totalmente descartável, previamente revisado.

## Executável autônomo (.exe) opcional

Na aba **Actions** do GitHub existe o workflow manual `Windows — testes e EXE`. Ele instala Python e dependências em uma máquina Windows do GitHub, roda os testes e tenta empacotar `pgddl.exe` com PyInstaller. O EXE é publicado como artefato daquela execução, **somente quando o workflow for acionado e concluído com sucesso**. Até que haja um resultado aprovado, use `install-windows.cmd` e `pgddl.cmd` como caminho suportado para testes.

## Diagnóstico de problemas

- `py` ou `python` ausente: instale Python 3.10+ x64 com venv e pip; verifique `py -3 --version` ou `python --version`.
- Falha no `pip install`: verificar proxy, acesso ao PyPI, certificado TLS e versões de Python/Windows.
- `connection refused`/timeout: confirmar IP, VPN, firewall, porta `-p` e serviço PostgreSQL.
- `no pg_hba.conf entry`: configurar autenticação no servidor **sem abrir a rede indevidamente**.
- `password authentication failed`: corrigir credencial libpq; não passe senha no comando nem envie arquivo de senhas ao suporte.
- Para interromper: Ctrl+C; consulte `status` antes de repetir `apply`. Não há rollback global. A retomada depende do mesmo arquivo, esquema, usuário e ID.
- Na 0.5.1, o relatório HTML é **manual** (subcomando `report`); a proposta de relatório automático da 0.5.2 não foi implementada nesta adaptação Windows.
