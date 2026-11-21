# PG DDL Runner 0.5.1

Executor de arquivos DDL recorrentes do fornecedor do ERP para PostgreSQL. Mantém a ordem original, confirma cada comando separadamente e registra seu checkpoint no mesmo banco e na mesma transação. Não substitui backup, revisão do SQL, teste em clone ou janela de manutenção. Não oferece rollback global nem modifica o arquivo do fornecedor.

## Novidades 0.5.1 — ajuda da linha de comando

Ajuda geral e de todos os oito subcomandos reorganizada em português, com descrição, parâmetros, valores padrão, exemplos, códigos de saída e alertas de segurança. Nenhuma mudança na execução, nos argumentos aceitos, valores padrão, manifesto ou schema de controle. O cabeçalho do relatório acompanha a versão. Use `./pgddl -h` e `./pgddl apply -h` (ou outro subcomando). A ajuda não conecta ao banco nem precisa de um arquivo DDL.

## Novidades 0.5.0 — auditoria e manutenção nativa

A execução permanece sequencial, sem alteração de parser, hash, esquema de histórico ou lógica de reconstrução de views. A atualização de laboratório ERP 508 Paraná concluiu os 2025 passos na 0.4.1. Instale em diretório separado; mantenha o mesmo arquivo, banco, schema, usuário e ID. Não limpe o histórico. O status agora usa snapshot consistente somente leitura e cruza falhas com checkpoints: `RESOLVIDA` quando executado, `RECONHECIDA MANUALMENTE` quando ack, `PENDENTE` quando não confirmado. Nenhuma falha é apagada. O status padrão exibe até cinco falhas pendentes e resume todas; `--history` exibe o histórico completo, `--migrations` lista migrações no schema deste banco. NOTICE tem nível INFO, não WARNING. A porta efetiva também aparece em conexões por socket.

```bash
./pgddl status comandos-erp-508-parana.ddl -d erp -U postgres -p 15777
./pgddl status comandos-erp-508-parana.ddl -d erp -U postgres -p 15777 --history --migrations
./pgddl report comandos-erp-508-parana.ddl -d erp -U postgres -p 15777 --validate
./pgddl validate comandos-erp-508-parana.ddl -d erp -U postgres -p 15777
```

`report` gera HTML local autônomo, sem rede, CSS embutido, escapando dados do SQL e das mensagens. Diretório padrão `~/.local/state/pgddl/reports` (ou XDG_STATE_HOME), substituível por `--report-dir /operacao/pgddl/reports`. Arquivo exclusivo com data local `DD-MM-AAAA_HH:MM`, sufixo único e permissão 0600. Contém identificação, status, contagem de falhas, ranking dos 30 passos mais demorados, tempo por família, checkpoints, histórico de migrações, reconhecimentos, retries bem-sucedidos, reconstruções de views e caminhos dos backups. Os nomes das views e configurações de manutenção só são registrados diretamente nos checkpoints a partir da 0.5; para reconstruções anteriores, consulte o views.json original pelo caminho registrado. As falhas terminais ficam no histórico; tentativas transitórias e amostras de sessões bloqueadoras não foram persistidas nas tabelas anteriores: consulte os logs locais. Tempos armazenados incluem espera e retry do passo confirmado, mas excluem o COMMIT final, pausas e tentativas definitivamente malsucedidas; não representam tempo de parede da migração inteira. `report` não altera o banco; o HTML pode conter SQL e metadados sensíveis, proteja seu acesso.

`validate` e `report --validate` examinam apenas expectativas dos passos confirmados (inclusive ack), por catálogo, sem executar SQL do fornecedor. Considera o último tipo solicitado para cada coluna; verifica existência de tabelas/sequences, colunas/tipos simples, índices B-tree simples (chaves, ordem, unicidade, validade e método). Não verifica definição completa de tabelas, constraints/defaults, opclasses/collations, dados, permissões, todos os índices complexos, views nem valor atual de sequences. `OK` significa somente que as propriedades verificadas conferem. `PARCIAL` e `NÃO VERIFICADO` exigem revisão conforme necessário; `DIVERGÊNCIA` retorna código 4. Uma migração incompleta valida só o subconjunto confirmado; não é certificação integral. Nenhuma correção automática. Use `--verbose` em validate para mostrar também resultados OK.

### Paralelismo nativo (opcional)

```bash
./pgddl apply comandos-erp-508-parana.ddl -d erp -U postgres -p 15777 \
  --dependency-policy rebuild-views --lock-policy retry \
  --parallel-maintenance-workers 4 --maintenance-work-mem 512MB
```

Os parâmetros acima são exemplos, não recomendações universais. Aplicados apenas à conexão executora via set_config; se omitidos, preservam os valores do servidor/role. Não executa ALTER SYSTEM nem modifica postgresql.conf, não reordena comandos nem abre executores DDL paralelos. A disponibilidade de workers e o plano interno dependem do PostgreSQL; no PG16 a criação paralela de índices aplica-se a B-tree. A memória configurada é o orçamento da operação de manutenção, não de cada worker. Maior paralelismo pode piorar I/O e latência do ERP. Faça benchmarks em clones equivalentes; uma migração já concluída não será reaplicada para benchmark. O limite pode ser zero para desabilitar workers de manutenção. As configurações efetivas são registradas no log e nos novos checkpoints.

### Testes 0.5.0

37 testes unitários locais aprovados. Manifesto dos 2025 comandos comparado com 0.4.1. Testes reais de integração da 0.5 ainda pendentes (ambiente de desenvolvimento sem servidor PostgreSQL/psycopg). O teste `integration_lab.py` também verifica relatórios e validação após uma falha resolvida. Use apenas banco descartável `pgddl_test`.

```bash
.venv/bin/python -m unittest -v test_pgddl test_viewdeps test_audit
PGPORT=15777 .venv/bin/python integration_lab.py --dbname pgddl_test --disposable
```

## Instalação (Linux)

Python 3.10 ou superior (preferência 3.12), com venv/pip e acesso a um repositório de pacotes para instalar psycopg 3. Em Rocky Linux 8/9 use um Python compatível instalado lado a lado, sem substituir o Python do sistema. O instalador não modifica serviços, não cria banco e não exige root.

```bash
tar xzf pg-ddl-runner-0.5.1.tar.gz
cd pg-ddl-runner-0.5.1
./install.sh
./pgddl --version
```

Para apenas inspecionar o arquivo sem instalar dependências ou conectar ao banco:

```bash
python3.12 pgddl.py plan /caminho/comandos-erp-508-parana.ddl
python3.12 pgddl.py plan /caminho/comandos-erp-508-parana.ddl --full
```

## Procedimento recomendado

1. Arquive o arquivo original e faça backup/snapshot consistente e validado do banco; teste o DDL no clone.
2. Pare sessões do ERP e outros escritores conforme a janela de manutenção; confira versão do ERP, banco, schema, dependências e espaço livre. O runner não bloqueia o ERP nem DDL externos.
3. Execute `plan` e revise comandos, particularmente reinícios de sequences, índices únicos e mudanças de tipo.
4. Configure autenticação PostgreSQL normalmente (socket local/peer ou PGHOST, PGPORT, PGUSER, ~/.pgpass com permissão 0600). Não passe senhas pela linha de comando. O usuário precisa de privilégios DDL no alvo e de criação/manutenção das tabelas de controle; use o mesmo papel em todas as retomadas.
5. Aplique o arquivo. O nome do banco solicitado será mostrado e solicitado como confirmação. Em automação use `--yes` explicitamente.

```bash
./pgddl plan comandos-erp-508-parana.ddl
./pgddl apply comandos-erp-508-parana.ddl -d erp -U postgres --schema public
```

Teste inicial em clone com pausa controlada após 20 novos comandos:

```bash
./pgddl apply comandos-erp-508-parana.ddl -d erp_lab -U postgres --max-steps 20
./pgddl status comandos-erp-508-parana.ddl -d erp_lab -U postgres
./pgddl apply comandos-erp-508-parana.ddl -d erp_lab -U postgres
```

Para servidor remoto acrescente `--host HOST --port 5432` (ou use variáveis libpq). Use configuração SSL adequada ao seu ambiente. O padrão `--schema public` fixa o search_path ao schema selecionado (pg_catalog implícito). Comandos explicitamente qualificados podem atingir outros schemas; revise SQL do fornecedor antes de executar. O histórico usa o schema reservado `_pgddl_runner`. O esquema-alvo deve existir.

## Correção 0.4.1 — permissões de lock em views

Caso real: `LOCK TABLE indikatore.itens IN ACCESS EXCLUSIVE MODE NOWAIT` retornou `permission denied for table produtos` mesmo com postgres, porque a verificação recursiva considera o owner da view. A 0.4.1 elimina esse pré-lock recursivo, mantém o pré-lock da tabela a alterar, o backup cronológico, DROP RESTRICT e a transação única com checkpoint. Logs BEGIN/OK identificam cada etapa interna. Não há mudança no manifesto ou schema de histórico. Validação local: 27/27 testes unitários aprovados, incluindo ausência de LOCK TABLE nas views e interrupção quando metadados mudam após o backup. O manifesto dos 2025 comandos continua idêntico ao da 0.4.0. A integração agora inclui owner com SELECT somente e reprodução do erro 42501; não executada neste ambiente por falta de PostgreSQL e psycopg. Consulte status e retome o passo pendente usando o mesmo arquivo, banco, schema e ID de migração. Materialized views continuam bloqueando a reconstrução automática de toda a cadeia.

## Novidades 0.4.0 — dependências e backup de views

O dry run identifica views e materialized views dependentes de `ALTER COLUMN TYPE` por dependências de coluna e regras `_RETURN`, inclusive transitivas e entre schemas. Não é uma auditoria de todas as dependências de objetos PostgreSQL. O modo `--locks-only` continua restrito a locks. Materialized views são destacadas com `!!! MATERIALIZED VIEW !!!`, estado de população e, na exportação, quantidade de índices. **Se qualquer materialized view fizer parte da cadeia, a reconstrução automática de toda a cadeia é recusada.** Não há DROP, REFRESH ou recriação automática de materialized views; não exporta seus dados.

### Inspecionar e exportar antes do apply

No cenário real de `public.grupos.grup_classificacao` (`varchar(20)` → `varchar(40)`), passo 20, porta 15777:

```bash
./pgddl dependencies comandos-erp-508-parana.ddl -d erp -U postgres -p 15777 --step 20 \
  --backup-dir /operacao/pgddl/views
```

A conexão é somente leitura. Não altera histórico nem executa DDL. Lista dependências em ordem de recriação e exporta os metadados. Código 4 se encontrar materialized view; demais falhas de diagnóstico retornam 1. A exportação é realizada a partir de um snapshot consistente de catálogo; não bloqueia a estrutura para uso posterior. O apply faz nova captura após adquirir o pré-lock da tabela, sem reutilizar arquivos antigos.

### Retomar com reconstrução autorizada

```bash
./pgddl status comandos-erp-508-parana.ddl -d erp -U postgres -p 15777
./pgddl apply comandos-erp-508-parana.ddl -d erp -U postgres -p 15777 \
  --dependency-policy rebuild-views --backup-dir /operacao/pgddl/views --max-steps 1
```

Mantém o histórico 0.3, o mesmo arquivo, o mesmo schema, usuário e identificação de migração. `--max-steps 1` executa apenas o próximo comando pendente. Sem o parâmetro, processa os passos seguintes normalmente. Por padrão `--dependency-policy stop` exporta o diagnóstico e pausa quando encontra dependências; nenhum DROP automático ocorre. `rebuild-views` autoriza a reconstrução das views comuns suportadas, não de materialized views.

Antes de qualquer DROP, o runner exige superusuário sem SET ROLE prévio, adquire o pré-lock da tabela alvo, verifica novamente o grafo, captura definições com search_path seguro e grava os arquivos localmente com fsync. A partir da 0.4.1 não executa LOCK TABLE nas views: esse comando bloqueia recursivamente as tabelas de origem e exige privilégios do owner da view (SQLSTATE 42501 reproduzido no PostgreSQL 16.15). Cada DROP VIEW RESTRICT adquire os próprios locks, respeitando o lock_timeout configurado (positivo obrigatório para reconstrução, padrão 3 s por lock). Não há NOWAIT garantido durante DROP/CREATE; não há retry automático dessas etapas. Após o backup, grafo e metadados são novamente comparados antes de iniciar os DROPs. **É obrigatório não realizar DDL concorrente sobre as views e suas dependências**: o pré-lock da tabela não impede toda alteração concorrente em views, e a comparação não elimina a janela entre leitura e DROP. Use janela de manutenção. Nenhuma sessão é encerrada e nenhum privilégio adicional é concedido nas tabelas de origem.

O runner derruba as views de fora para dentro (`DROP VIEW ... RESTRICT`), executa o DDL original, recria de dentro para fora, restaura owner, grants de tabela/coluna emitidos pelo owner (incluindo grant option), comentários e opções de view suportadas. Valida novamente o catálogo e grava o checkpoint dentro da mesma transação. Qualquer divergência, falha de backup, lock, DDL, grant, validação ou checkpoint aborta o passo; os anteriores não sofrem rollback. Por segurança são recusados triggers, regras adicionais, security labels, extensões, defaults de coluna, grantors não proprietários, metadados não suportados e alvos com herança/partições. `RESTRICT` impede o descarte de dependências externas normais não previstas. A interface das views mantém nomes, tipos e collation; admite apenas ampliação de varchar como mudança de tipo de saída, com alerta. Validação de catálogo não comprova resultado semântico, desempenho ou compatibilidade com aplicação. O OID das views muda; teste integrações e prepare janela de manutenção. Não substitui pg_dump/snapshot validado.

### Histórico de arquivos

Padrão: `~/.local/state/pgddl/views` (ou XDG_STATE_HOME). Cada captura cria diretório no formato `17-11-2026_14:55_step-0020_<sufixo-único>` usando o fuso local do executor, incl. UTC offset no JSON. Nenhuma captura é sobrescrita. Diretório novo 0700 e arquivos 0600. Use `--backup-dir` para guardar em local protegido e incluído no backup operacional. O formato com dois-pontos é para Linux.

- `views.json`: catálogo capturado (definições, owners, ACLs, colunas, comentários, opções, índices de matviews, população, limitações, grafo, SQL original, hash e timestamp).
- `restore-views.sql`: somente para cadeia suportada de views comuns. Cria objetos **ausentes** sem DROP e sem alterar a tabela. Revisar antes de restaurar, usando `psql -X -v ON_ERROR_STOP=1 -f ...` no banco correto, como superusuário. Não executar após sucesso do apply nem sobre views existentes.
- `LEIA-ME.txt`: em vez do SQL de restauração quando houver materialized view ou condição não suportada. O JSON é apenas diagnóstico, não backup restaurável completo; use pg_dump e procedimento manual.

Os arquivos sobrevivem mesmo quando a transação do banco falha. O checkpoint concluído registra o caminho do backup e a quantidade de views reconstruídas no JSON de detalhe. Nenhuma limpeza automática do histórico local.

### Validação 0.4.0

25 testes unitários aprovados (20 anteriores + 5 de grafo, alerta, interface e backup). Os 2025 comandos originais mantêm hash e sequência. O dry run 0.2 e o checkpoint após erro no passo 20 da 0.3 foram validados pelo operador. O novo teste de integração `integration_views.py` cobre quatro views, dependências indiretas e entre schemas, grants, comentários, retomada e recusa de materialized view com índice; não foi executado aqui por falta de PostgreSQL e psycopg. Execute somente em banco descartável chamado `pgddl_test`, como superusuário, usando configurações libpq para conexão:

```bash
.venv/bin/python -m unittest -v test_pgddl test_viewdeps
PGPORT=15777 .venv/bin/python integration_views.py --dbname pgddl_test --disposable
```

Não use a reconstrução em produção antes de homologar no clone.

## Novidades 0.3.0 — espera inteligente e análise estrutural

`nowait` continua padrão; o novo modo é opt-in. Exemplo em clone:

```bash
./pgddl apply comandos-erp-508-parana.ddl -d erp_lab -U postgres \
  --lock-policy retry --retry-interval 5 --retry-timeout 300 --max-steps 20
```

Em `retry`, só uma falha SQLSTATE 55P03 durante o pré-lock, antes da execução do DDL, pode acionar uma tentativa nova. Após rollback confirmado e conexão ociosa, aguarda fora da transação, sem lock de tabela. O advisory lock de execução do runner permanece. O prazo (300 s padrão) é por comando, não pela migração inteira; limita as novas tentativas, não o tempo de DDL, diagnósticos ou locks adicionais. Não há retry de falhas do DDL, deadlock, erro de commit/checkpoint ou perda de conexão. A tentativa que obtiver o lock executa o DDL e grava o checkpoint na mesma transação. O histórico inclui `lock_attempts` no JSON de detalhe dos novos comandos; não exige alteração de tabelas. Falhas intermediárias ficam no log local; somente falha terminal é registrada em `errors`. Ctrl+C preserva os checkpoints anteriores. Nenhum processo é encerrado, nenhuma alteração é reordenada. O modo não evita bloqueio durante o próprio DDL. Use janela de manutenção.

```bash
./pgddl dry-run comandos-erp-508-parana.ddl -d erp_lab -U postgres \
  --verbose --log precheck-v03.log
# Para reproduzir somente o diagnóstico de locks da 0.2:
./pgddl dry-run comandos-erp-508-parana.ddl -d erp_lab -U postgres --locks-only
```

A análise estrutural (padrão) acrescenta:

- `COLUNA_EXISTENTE`: tipo atual e esperado para tipos simples, presença de default e NOT NULL. Mesmo tipo não significa equivalência. Colunas repetidas no arquivo e alvos de ALTER TYPE ausentes também são sinalizados.
- `NOME_INDICE_EXISTENTE`: nome já utilizado no schema da tabela existente. `COLUNA_INDICE_AUSENTE` verifica chaves simples em tabelas existentes, considerando ADD COLUMN anteriores no arquivo.
- `INDICE_CANDIDATO_REDUNDANTE` e `INDICE_CANDIDATO_NO_ARQUIVO`: chaves simples, mesma ordem, unicidade e B-tree. Exclui índices existentes parciais, de expressão, INCLUDE, inválidos e com ordenação não padrão. Não compara todos os detalhes (opclasses, collations, opções de storage, NULLS NOT DISTINCT, vínculos a constraints etc.). Candidatos não são equivalência comprovada. Formas SQL complexas não são comparadas. Não remove, ignora ou reconhece automaticamente objetos.
- `ALTER_TYPE_REVISAR`, `INDICE_UNICO_REVISAR`, `SEQUENCE_EXISTENTE_REVISAR`: alertas de impacto, possíveis reescritas, duplicidades de dados ou reutilização de valores. Não examina dados.
- `IMPACTO_ESTIMADO`: tamanho aproximado do heap (`relpages × block_size`) e `reltuples` para ALTER TYPE e índices, sem varrer a tabela. Catálogo pode estar desatualizado, valores zero ou -1 podem indicar ausência de estatística; não inclui índices, TOAST ou descendentes de partições. Não estima tempo ou espaço livre.

O relatório é indicativo e não valida toda a estrutura futura (CREATE TABLE não tem suas colunas interpretadas, tipos complexos não são comparados). Código 4 agora também inclui alertas estruturais, inclusive candidatos a redundância; avisos de impacto informativos isolados não alteram o retorno. O dry run mantém conexão somente leitura e não pede LOCK TABLE; consultas de catálogo ainda usam locks internos normais. `--locks-only` preserva a análise anterior.

## Compatibilidade e validação 0.3.0

Hashes, manifesto, schema de controle e formato anterior preservados. Não é necessário migrar o histórico. A versão 0.2 teve dry run aprovado pelo operador; apply ainda não testado pelo operador. A 0.3 tem 20 testes unitários aprovados, incluindo simulados de análise estrutural e política de retry. O arquivo original continua com 2025 comandos e mesmo SHA-256. O script de integração inclui cenários de expiração do retry, liberação do lock e confirmação única, mas ainda não foi executado por falta de PostgreSQL e psycopg neste ambiente. Comece em clone descartável e não em produção.

## Novidades 0.2.0 — dry run e prevenção de filas de lock

```bash
./pgddl dry-run comandos-erp-508-parana.ddl -d erp -U postgres
./pgddl dry-run comandos-erp-508-parana.ddl -d erp -U postgres --max-steps 20 --verbose
./pgddl apply comandos-erp-508-parana.ddl -d erp -U postgres --max-steps 20
```

`dry-run` abre conexão somente leitura (`default_transaction_read_only=on`), lê catálogo e histórico se houver, não cria schema de controle, não executa DDL nem `LOCK TABLE`, não tenta um rollback global. Analisa os passos pendentes, reaproveitando a fotografia de locks por alvo e modo. Distingue `LOCK_POTENCIAL`, `SEM_CONFLITO_VISIVEL`, `ALVO_AUSENTE`, `TIPO_INESPERADO`, `EXISTENTE` (criação de tabela/sequence), `CRIACAO_PREVISTA` e `DEPENDENCIA_PREVISTA` (tabela/sequence criada antes no mesmo arquivo). Por padrão registra os alertas e o resumo; `--verbose` lista todos. Código 4 sinaliza alertas. Na 0.2 (ou com --locks-only) não verifica previamente duplicidade de coluna/índice. A 0.3 acrescenta análise parcial; não verifica sintaxe completa, dados existentes, permissões, todas as dependências ou espaço. Não garante sequer que o lock permanecerá livre; os estados são fotos indicativas, não um certificado. Execute com o mesmo papel da migração para verificar histórico. O log local é gravado normalmente.

`apply` usa por padrão `--lock-policy nowait`: antes de ALTER TABLE adquire ACCESS EXCLUSIVE NOWAIT; antes de CREATE INDEX normal/UNIQUE, SHARE NOWAIT. O lock, DDL e checkpoint ficam na mesma transação. ALTER TABLE inclui descendentes salvo ONLY; criação de índice inclui descendentes para tabela particionada salvo ON ONLY. O diagnóstico considera locks relacionais conflitantes concedidos ou pendentes, excluindo a própria conexão; uma transação preparada sem PID também aparece. Para sequences, objetos novos, objetos especiais e locks adicionais, prevalece `lock_timeout` (3 s por lock por padrão); NOWAIT não cobre todo DDL. Se houver falha, diagnostica os conflitos ainda visíveis (podem ter desaparecido), grava erro e para, sem pular, matar sessões, cancelar ou reordenar comandos. Retome `apply` após resolver a causa. `--lock-policy wait --lock-timeout 10` desativa o pré-lock explícito e permite espera normal limitada por lock; use só intencionalmente. Nenhum modo elimina o bloqueio durante DDL efetivo. Índices normais podem bloquear escrita por muito tempo; ALTER TYPE pode reescrever tabelas. Mantenha janela de manutenção.

O monitor usa uma segunda conexão PostgreSQL somente leitura a cada 2 s (ajustável `--monitor-interval 5`, 0 desativa), consultando o PID do executor, eventos de espera, bloqueadores reais via `pg_blocking_pids` e `pg_stat_progress_create_index`. Pode não enxergar esperas muito curtas; sem monitor, o diagnóstico de locks após erro continua funcionando. Se o monitor falhar, o DDL continua com os timeouts configurados. Para detalhes de outras sessões, use superusuário ou papel com `pg_read_all_stats` conforme política local; informações podem ser ocultadas caso contrário. Não são registradas consultas das sessões bloqueadoras, somente metadados (PID, usuário, IP, aplicação, estado, idade da transação, evento de espera). Não concede privilégios automaticamente.

A estrutura das tabelas de controle continua compatível com a 0.1.0; não há alteração do formato do manifesto ou dos hashes. O teste de integração foi ampliado para simular SELECT aberto, escrita, erro NOWAIT e retomada, ainda precisa rodar em PostgreSQL de laboratório.

## Pausa, falha, retomada

- Ctrl+C interrompe o processo; o PostgreSQL cancela ou encerra o comando/conexão conforme a situação. Consulte `status` antes de retomar. Se houve commit antes da interrupção, checkpoint e DDL estão registrados juntos; caso contrário, DDL transacional e checkpoint não foram confirmados. Não há tentativa automática após erro/COMMIT incerto.
- Um erro para no primeiro comando com falha, mostra SQLSTATE, SQL completo e linha do arquivo. Os comandos confirmados anteriormente são mantidos. Corrija a causa no banco (por exemplo aguardar/remover bloqueio, corrigir dados com orientação do fornecedor), depois use `apply` com o MESMO arquivo, ID, encoding, schema e papel. Não edite o original em andamento; alteração de um byte bloqueia a retomada. Se o SQL do fornecedor estiver incorreto e for impossível executar o original, não force o hash ou altere o histórico: combine um procedimento de correção documentado e valide em clone; o suporte a revisão formal de manifesto fica para versão futura.
- WARNINGS do servidor provocam pausa após a confirmação do comando por padrão; revise o log antes de retomar. NOTICEs são exibidos; não interrompem automaticamente. `--continue-on-warning` altera esse comportamento.
- `--lock-timeout 3` (padrão, segundos) evita aguardar indefinidamente locks; `--statement-timeout 0` (padrão, sem limite) permite índices longos. Configure limites adequados ao ambiente. `--max-steps N` pausa após N novos comandos confirmados, não N comandos absolutos.
- O log local é criado por padrão em `~/.local/state/pgddl/logs/` (ou `$XDG_STATE_HOME/pgddl/logs`). `--log /caminho/execucao.log` permite escolher o arquivo. `--verbose` mostra cada SQL integral. Proteja o log, pois contém SQL e mensagens do servidor. `status` pode consultar a parte já confirmada enquanto apply segue executando.
- Uma segunda instância do runner no mesmo banco é bloqueada por advisory lock; não bloqueia psql ou aplicações que não cooperam com esse lock. Nova migração no mesmo schema é bloqueada se outra registrada estiver incompleta. O ID padrão é o nome do arquivo; opcionalmente use `--migration-id ERP-5.08.00-PR` em todas as chamadas (inclusive a primeira). A mesma impressão digital não pode ganhar outro ID no mesmo schema. Isso não detecta automaticamente arquivos cumulativos diferentes.
- Depois da conclusão, rodar apply novamente não reexecuta os comandos. Se o banco for restaurado, o histórico deve ser restaurado junto, a partir do mesmo ponto consistente.

### Comando já aplicado manualmente

O runner NÃO ignora automaticamente `duplicate_column`, `duplicate_table`, `duplicate_object` nem qualquer SQLSTATE. Um objeto com o mesmo nome pode ter configuração/definição diferente. Se o operador verificar que o efeito exato do próximo comando já existe (ou executá-lo manualmente com correção aprovada), pode reconhecê-lo explicitamente, com evidência e motivo:

```bash
./pgddl status comandos-erp-508-parana.ddl -d erp -U postgres
./pgddl ack comandos-erp-508-parana.ddl -d erp -U postgres --step 123 \
  --reason 'Definição conferida com fornecedor; ticket XYZ' --yes
./pgddl apply comandos-erp-508-parana.ddl -d erp -U postgres
```

O número 123 é apenas exemplo: deve ser o próximo pendente real. `ack` não executa SQL, não verifica equivalência automaticamente e não deve ser usado para ignorar erros ou reinícios de sequence sem prova. O registro explicita modo `acknowledged`, separado de `executed`.

## Monitoramento PostgreSQL durante um índice

O runner mostra o PID da conexão. Em outro terminal, com permissão adequada:

```sql
SELECT pid, relid::regclass AS tabela, phase,
       blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;

SELECT pid, state, wait_event_type, wait_event,
       clock_timestamp() - query_start AS duracao,
       pg_blocking_pids(pid) AS bloqueadores, query
FROM pg_stat_activity
WHERE application_name = 'pgddl-runner';
```

A porcentagem de comandos não é previsão de tempo: um único índice em uma tabela grande pode consumir mais tempo do que centenas de ALTER TABLE ADD COLUMN.

## Escopo e limites desta versão

- O arquivo real foi reconhecido: 2025 comandos, 2795 linhas, CP1252, SHA256 `85770f5beea1af81100dd12c665959dab50d5f534f8b0ac2fa2fbf0b26dedff1`.
- Suporta famílias do arquivo: CREATE TABLE simples, ALTER TABLE ADD COLUMN e ALTER COLUMN TYPE, CREATE INDEX/UNIQUE INDEX, CREATE SEQUENCE e ALTER SEQUENCE RESTART. Não oferece parser PostgreSQL completo: regras conservadoras, rejeita famílias desconhecidas, IF EXISTS/IF NOT EXISTS, operações CONCURRENTLY, comandos compostos e alguns formatos de literais. Validação offline classifica e separa comandos; não valida toda a gramática, dependências, permissões ou efeitos. O PostgreSQL valida a sintaxe na execução; SQL confiável do fornecedor é pressuposto. Evoluir o escopo explicitamente caso o fornecedor adicione outros comandos. Não execute arquivos de origem desconhecida.
- DDL transacional e checkpoint são atômicos, mas funções com efeitos externos e operações de sequence não transacionais (ex. nextval) não têm a mesma garantia; não estão presentes como chamadas no arquivo examinado. `ALTER SEQUENCE RESTART` é transacional, mas pode ser perigoso em sequence já utilizada. Não retome com o ERP em atividade sem avaliação.
- Não converte índices para CONCURRENTLY nem adiciona IF NOT EXISTS. Isso alteraria a semântica do fornecedor e, no caso de CONCURRENTLY, impediria o checkpoint na mesma transação.
- Não há validação estrutural automática pós-execução nem cálculo de espaço livre/duração. Mantenha backup e valide a estrutura e ERP antes de liberar produção. Tabelas de controle devem ter alterações restritas a administradores confiáveis.
- PostgreSQL 13–18 é o alvo pretendido, não certificação. Testes offline executados; integração real ainda pendente.

## Histórico e códigos de saída

`_pgddl_runner.migrations`: identidade, hash, total, encoding e papel.
`_pgddl_runner.applied`: número, linha, hash, tipo, duração, modo, detalhes e data.
`_pgddl_runner.errors`: erros SQL (quando a conexão permite registrar).

```sql
SELECT * FROM _pgddl_runner.migrations ORDER BY created_at DESC;
SELECT * FROM _pgddl_runner.errors ORDER BY occurred_at DESC LIMIT 20;
```

4 = dry run com anomalias potenciais ou candidatos estruturais; 0 = sucesso ou pausa programada; 1 = falha de preparação/controle; 2 = erro SQL durante DDL/checkpoint; 3 = pausa após warning; 130 = Ctrl+C. Consulte sempre `status`: 0 também pode significar pausa programada.

## Testes

```bash
python3.12 -m unittest -v test_pgddl.py
# Somente em servidor/banco descartável, após configurar autenticação:
createdb pgddl_test
.venv/bin/python integration_lab.py --dbname pgddl_test --disposable
```

A integração cria vários objetos e histórico de teste em `pgddl_test`, deliberadamente preservados. Nunca indique um banco real. Remova o banco descartável quando não precisar mais dele, somente após confirmar o servidor e nome. O teste está incluído mas não foi executado no ambiente de criação (não há PostgreSQL/psycopg disponíveis).
