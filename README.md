# MiniPACS

MiniPACS é um receptor DICOM C-STORE temporário e uma interface web administrativa simples. Ele armazena estudos por `StudyInstanceUID`, elimina estudos após uma retenção configurável (15 dias por padrão) e não expõe arquivos DICOM diretamente pelo Nginx.

> AE Title sozinho não é autenticação forte. Esta primeira versão aceita um Calling AE Title configurado, mas não oferece TLS DICOM, VPN nem filtro de IP. Proteja a porta DICOM com firewall e planeje TLS/VPN antes de usar dados clínicos em produção.

## Arquitetura

```text
Modalidade/PACS -- C-STORE :11112 --> dicom-receiver --> data/ + SQLite
                                                    cleanup --> retenção/ZIPs antigos
Navegador -- HTTPS --> Nginx do host --> 127.0.0.1:8787 --> web/FastAPI
```

Os três serviços (`web`, `dicom-receiver`, `cleanup`) usam a mesma imagem Python, mas são processos separados. SQLite opera em WAL, com `busy_timeout`, transações curtas e chaves estrangeiras. O web é publicado exclusivamente em `127.0.0.1:8787`; a porta DICOM é publicada em todas as interfaces.

## Árvore

```text
app/                 FastAPI, SCP DICOM, SQLite, retenção e logging
templates/ static/   Interface sem framework frontend
config/              Templates Nginx isolados do MiniPACS
tests/               Pytest e emissor de estudo sintético
data/                DICOMs: StudyUID/SeriesUID/SOPUID.dcm (persistente)
db/                  minipacs.sqlite3 (persistente)
tmp/                 ZIPs transitórios (persistente, limpo após 24h)
logs/                web.log, dicom.log, cleanup.log com rotação
Dockerfile / docker-compose.yml
install.sh update.sh manage.sh
```

`data`, `db`, `tmp` e `logs` são bind mounts: `docker compose down` não remove dados. As pastas de dados são ignoradas pelo Git e pelo contexto de build. O container usa UID/GID 10001, nunca root.

## Teste local no Windows

Requer Python 3.12+ e Docker Desktop para teste integrado. No PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:COOKIE_SECURE = "false"
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD_HASH = ("defina um hash Argon2")
$env:SESSION_SECRET = "uma-chave-aleatoria-longa"
$env:ALLOWED_CALLING_AE = "TESTSCU"
pytest -q
python -m app.web
```

Para executar por Docker no Windows, copie `.env.example` para `.env`, preencha um hash Argon2 válido (gere com `python -m app.password_hash`, digitando a senha) **entre aspas simples**, defina `COOKIE_SECURE=false`, e execute `docker compose up --build`. O site fica em `http://127.0.0.1:8787` e o receptor em `localhost:11112`.

Em outro terminal, com o ambiente Python ativo:

```powershell
python tests/send_test_study.py --host 127.0.0.1 --port 11112 --calling-ae TESTSCU --called-ae MINIPACS --images 3
```

O utilitário gera apenas pixels e identificadores fictícios. Ele faz C-ECHO e C-STORE, mostra os status e cria uma linha de estudo na interface.

## Instalação Ubuntu/VPS

1. Copie esta árvore para `/root/minipacs` (preservando arquivos ocultos, inclusive `.gitattributes`).
2. Verifique que Docker, Docker Compose v2, Nginx e `curl` estão disponíveis. Para emissão automática, instale também `certbot` e garanta que DNS do domínio aponta para a VPS e as portas 80/443 chegam nela.
3. Execute o único comando inicial:

```bash
cd /root/minipacs
bash install.sh
```

O instalador exige root, cria os diretórios persistentes sob `/root/minipacs`, configura proprietários `10001:10001`, solicita domínio, AE Titles, porta e credenciais, gera Argon2 + `SESSION_SECRET`, constrói os serviços e valida `/health`. O `.env` é `root:root` e modo `0600`.

Ele adiciona somente `/etc/nginx/sites-available/minipacs` e o respectivo symlink. Antes de cada reload, executa `nginx -t`; se o certificado ainda não existir, instala uma configuração HTTP temporária para ACME, sem referenciar certificados inexistentes. Depois que o DNS estiver funcional:

```bash
certbot certonly --webroot -w /var/www/html -d SEU_DOMINIO
cd /root/minipacs && bash install.sh
```

O segundo comando troca a configuração isolada para HTTPS. O instalador não altera regras de UFW; quando UFW está ativo, ele apenas mostra a regra necessária para a porta DICOM.

## Configuração do remetente

Configure a modalidade/PACS assim (ajuste os valores escolhidos durante a instalação):

```text
IP/Host:         IP público ou hostname da VPS
Port:            11112
Called AE Title: MINIPACS
Calling AE Title: valor de ALLOWED_CALLING_AE
```

Vários Calling AEs podem ser separados por vírgula. Um Calling AE diferente é rejeitado antes de receber imagens. O receptor também aceita C-ECHO. Verifique a porta no host com `ss -ltnp | grep 11112` e teste com `tests/send_test_study.py`.

### Dois destinos no mesmo IP e porta

É possível separar os estudos de dois usuários sem abrir outra porta. Mantenha o AE principal e acrescente os outros destinos no `.env`:

```text
DICOM_AE_TITLE=VITOR
DICOM_EXTRA_AE_TITLES=FELIPE
```

Cadastre no PACS de origem dois destinos com o mesmo host e porta, mudando apenas o **Called AE Title**: `VITOR` ou `FELIPE`. O MiniPACS aceita apenas esses destinos e grava o Called AE como `destination_ae` em cada estudo; ele aparece na tabela administrativa e no retorno da API externa. Para o portal, use `GET /api/v1/studies?destination_ae=VITOR` (ou `FELIPE`) no backend, após autorizar o usuário. O mesmo `StudyInstanceUID` não pode ser recebido por dois destinos diferentes, evitando mistura acidental de acervos.

## Operação

```bash
./manage.sh status
./manage.sh logs
./manage.sh logs web
./manage.sh logs dicom-receiver
./manage.sh restart
./manage.sh stop
./manage.sh start
./manage.sh health
./manage.sh usage
```

A página consulta `/api/studies` a cada 30 segundos sem recarregar, mantém seleções quando possível e suporta busca, ZIP individual/em lote e exclusão confirmada. Cookies são HttpOnly, SameSite Strict e `Secure` quando `COOKIE_SECURE=true`; exclusões e operações em lote exigem token CSRF. Downloads passam pela aplicação autenticada e os ZIPs são criados em `tmp/`, nunca em `/tmp`.

## API externa para portal próprio ou Lovable

O MiniPACS mantém a interface administrativa atual e oferece uma API externa separada, inicialmente desabilitada. A API usa token Bearer com hash Argon2, não aceita exclusão e não compartilha a sessão administrativa do navegador.

Gere uma credencial na VPS. Copie o token somente para o cofre de segredos do backend do seu portal; ele não deve ser colocado em JavaScript, variáveis públicas do Lovable ou código entregue ao navegador.

```bash
cd /root/minipacs
docker compose run --rm --no-deps -T web python -m app.api_key
```

O comando exibe o token uma única vez e uma linha `EXTERNAL_API_TOKEN_HASH='...'`. Adicione essa linha ao `.env` junto com um nome para auditoria e a origem HTTPS exata do portal:

```text
EXTERNAL_API_TOKEN_NAME=lovable-backend
EXTERNAL_API_ALLOWED_ORIGINS=https://app.seudominio.com
EXTERNAL_API_RATE_LIMIT_PER_MINUTE=60
```

Depois recrie o serviço web:

```bash
docker compose up -d --force-recreate web
```

Os endpoints são publicados no mesmo domínio HTTPS do MiniPACS:

```text
GET /api/v1/studies?q=texto&destination_ae=VITOR
GET /api/v1/studies/{StudyInstanceUID}
GET /api/v1/studies/{StudyInstanceUID}/download
```

Todas as chamadas exigem:

```http
Authorization: Bearer mpk_seu_token_secreto
```

Exemplo de teste a partir de um backend confiável:

```bash
curl -H "Authorization: Bearer $MINIPACS_API_TOKEN" \
  https://SEU_DOMINIO/api/v1/studies
```

O CORS aceita apenas as origens declaradas em `EXTERNAL_API_ALLOWED_ORIGINS`, mas CORS não substitui autenticação. O portal deve autenticar seus próprios usuários, aplicar suas permissões e chamar a API a partir do seu backend. Vincule laudos a `StudyInstanceUID`; mantenha os arquivos DICOM na VPS.

Para revogar uma integração, gere outro token, troque `EXTERNAL_API_TOKEN_HASH`, recrie `web` e remova o token antigo do portal. Cada download externo fica registrado no audit como `API_DOWNLOAD`.

## Endurecimento da implantação

O receptor valida UIDs DICOM antes de criar caminhos no disco, exige Called AE Title compatível com `DICOM_AE_TITLE`, limita associações simultâneas e aplica `MAX_DICOM_FILE_BYTES` por instância. O limite padrão é 512 MiB; ajuste-o somente se a modalidade precisar de arquivos maiores.

Os containers `dicom-receiver` e `cleanup` não recebem mais a senha administrativa, o segredo de sessão ou o token da API. O Docker pode publicar portas antes das regras convencionais do UFW; para limitar a porta DICOM por IP, use o firewall do provedor ou regras na cadeia `DOCKER-USER`, conforme a [documentação do Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/). Para dados clínicos em produção, mantenha `8042` permitido somente para os IPs conhecidos das modalidades e planeje DICOM TLS ou VPN.

Para atualizar apenas o código substituído, sem tocar em `.env`, `data`, `db`, `logs` ou `tmp`:

```bash
./update.sh
```

Para alterar retenção, edite `RETENTION_DAYS` no `.env` e execute `docker compose up -d`; novos recebimentos recalculam `retention_until` a partir do último recebimento. Para trocar senha, gere um novo hash usando `docker compose run --rm --no-deps -T web python -m app.password_hash`, substitua apenas `ADMIN_PASSWORD_HASH` no `.env` e rode `docker compose up -d`. Faça backup com serviços parados ou snapshot consistente de `data/`, `db/` (incluindo WAL/SHM se presentes) e `.env` protegido.

Logs também aparecem em `docker compose logs`; arquivos rotacionados ficam em `logs/`. Não faça upload de DICOM pelo navegador: esta versão só recebe C-STORE e não implementa DICOMweb, viewer, C-FIND/MOVE/GET, WebSocket ou SSE.
