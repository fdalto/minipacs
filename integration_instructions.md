# Instruções de integração — Portal Lovable + MiniPACS

Leia este documento antes de implementar. O MiniPACS já recebe arquivos DICOM na VPS e é a fonte de verdade para estudos e arquivos. Este portal deve ser uma interface de usuários, permissões, laudos e experiência de uso; **não deve receber nem armazenar DICOM diretamente**.

## Objetivo

Criar um portal web responsivo em português (Brasil) onde usuários autenticados visualizam somente os exames destinados a eles, pesquisam estudos e, se tiverem permissão, baixam o ZIP DICOM de um estudo.

Os estudos são separados no MiniPACS pelo `destination_ae`:

| Usuário/grupo | `destination_ae` |
|---|---|
| Vitor | `VITOR` |
| Felipe | `FELIPE` |

Essa coluna é definida pelo **Called AE Title** usado pela modalidade/PACS ao enviar o estudo para a VPS. Não permita que o frontend escolha livremente esse valor: ele deve vir do perfil/permissão do usuário autenticado.

## Arquitetura obrigatória

```text
Navegador
  -> autenticação e páginas do portal Lovable
  -> backend / Edge Function privado do Lovable
  -> HTTPS + Bearer token -> MiniPACS na VPS
```

O navegador nunca pode chamar o MiniPACS diretamente usando um token e nunca pode receber o token em JavaScript, HTML, logs, variáveis públicas (`VITE_*`, `NEXT_PUBLIC_*`) ou banco de dados visível ao cliente.

O backend/Edge Function do Lovable deve manter o token em um segredo de servidor chamado, por exemplo, `MINIPACS_API_TOKEN`.

## Configuração do MiniPACS

Base URL da VPS:

```text
https://minipacs-vitor.duckdns.org
```

O administrador da VPS gera o token uma vez com:

```bash
cd /root/minipacs
docker compose run --rm --no-deps -T web python -m app.api_key
```

Ele adicionará o hash exibido ao `.env` da VPS e entregará o valor `mpk_...` ao cofre de segredos do Lovable. O token é mostrado somente nessa geração; se vazar, deve ser rotacionado.

Todas as chamadas do backend do Lovable devem enviar:

```http
Authorization: Bearer <MINIPACS_API_TOKEN>
Accept: application/json
```

O serviço só deve ser usado por HTTPS. Não usar a porta DICOM `8042` nesta integração; ela é exclusiva para modalidades/PACS. A API usa HTTPS normal na porta `443`.

## Contrato da API MiniPACS

### Listar estudos

```http
GET /api/v1/studies?q=<busca>&destination_ae=<AE>
```

Exemplo que o backend usará para Vitor:

```text
GET https://minipacs-vitor.duckdns.org/api/v1/studies?destination_ae=VITOR&q=joao
```

Retorno:

```json
{
  "studies": [
    {
      "study_instance_uid": "1.2.840...",
      "patient_id": "123",
      "patient_name": "NOME DO PACIENTE",
      "study_date": "20261007",
      "study_time": "143000",
      "accession_number": "ACC-123",
      "study_description": "TC TÓRAX",
      "modality": "CT",
      "destination_ae": "VITOR",
      "received_at": "2026-10-07T17:30:00+00:00",
      "last_received_at": "2026-10-07T17:31:00+00:00",
      "image_count": 243,
      "total_size_bytes": 12345678,
      "status": "ready"
    }
  ],
  "summary": { "studies": 1, "images": 243, "bytes": 12345678 }
}
```

### Detalhar estudo

```http
GET /api/v1/studies/{study_instance_uid}
```

Antes de devolver o resultado, o backend deve confirmar que `destination_ae` do estudo pertence ao usuário autenticado. Não basta confiar no UID recebido pela URL.

### Baixar ZIP DICOM

```http
GET /api/v1/studies/{study_instance_uid}/download
```

O backend deve validar a permissão e o `destination_ae` antes de buscar esse endpoint. Faça streaming da resposta para o usuário autenticado, preservando `Content-Type` e propondo um nome de arquivo seguro. Nunca redirecione o navegador para a URL do MiniPACS com o token como query string.

### Respostas de erro

| Código | Significado | Comportamento do portal |
|---|---|---|
| 401 | token ausente/inválido | registrar erro técnico sem vazar token; mostrar erro genérico |
| 404 | API não habilitada ou estudo inexistente | mostrar “não encontrado” |
| 429 | limite de requisições | avisar e permitir nova tentativa após alguns segundos |
| 5xx | falha temporária da VPS | mostrar estado de indisponibilidade; não inventar dados |

## Autenticação e autorização do portal

Implemente autenticação própria do portal (por exemplo, a solução de autenticação configurada no Lovable). Mantenha uma tabela de perfil/role no backend com uma lista controlada de destinos permitidos, por exemplo:

```text
user_id | role     | allowed_destination_aes
---------|----------|------------------------
...      | vitor    | ["VITOR"]
...      | felipe   | ["FELIPE"]
...      | admin    | ["VITOR", "FELIPE"]
```

Regras obrigatórias:

1. Usuário comum só recebe dados dos destinos autorizados.
2. O backend determina o filtro `destination_ae`; não aceite esse valor do cliente sem validar contra o perfil.
3. Para administradores, o backend pode listar cada destino separadamente ou aceitar uma seleção validada.
4. Todas as ações de download devem gerar um registro de auditoria no banco do portal: usuário, data/hora, `StudyInstanceUID`, destino e resultado.
5. Não exibir nem registrar no navegador o token Bearer, URLs autenticadas ou dados DICOM brutos.

Importante: `destination_ae` organiza os estudos, mas não é uma credencial criptográfica. A proteção de acesso do portal deve ser implementada pela autenticação e autorização próprias.

## Endpoints internos sugeridos do portal

Crie rotas/Edge Functions privadas, protegidas pela sessão do usuário:

```text
GET  /api/portal/studies?q=...
GET  /api/portal/studies/:studyInstanceUid
GET  /api/portal/studies/:studyInstanceUid/download
POST /api/portal/reports
GET  /api/portal/reports?study_instance_uid=...
```

As três primeiras são proxies seguros para a VPS. Os laudos pertencem ao banco do portal e devem referenciar `study_instance_uid`; eles não devem alterar os arquivos na VPS.

Pseudocódigo para listagem no servidor:

```ts
const user = await requireAuthenticatedUser(request);
const allowedAes = await allowedDestinationAesFor(user.id);

// Para um usuário comum, o valor vem do perfil, não do query string do browser.
const destinationAe = allowedAes.length === 1 ? allowedAes[0] : validateRequestedAe(request, allowedAes);

const response = await fetch(
  `${MINIPACS_BASE_URL}/api/v1/studies?` + new URLSearchParams({
    destination_ae: destinationAe,
    q: safeSearchText(request.query.q),
  }),
  { headers: { Authorization: `Bearer ${MINIPACS_API_TOKEN}`, Accept: "application/json" } },
);

return response.json();
```

## Interface desejada

- Página de login de usuários com pagina de admin para cadastro do user e associação ao grupo desejado e reset de senha.
- Página “Exames” com busca por paciente, ID, descrição, accession e data.
- Organização e filtro da lista exibida por data ou AETITLE (Felipe e Vitor) e outros parametros.
- Tabela com paciente, data, modalidade, descrição, quantidade de imagens, recebido em e status.
- Exibir o destino somente para administradores; para usuário comum ele é implícito à sua associação.
- Estados claros para carregamento, nenhum resultado, erro e API temporariamente indisponível.
- Datas em formato brasileiro e tamanhos de arquivo legíveis.
- Não criar um visualizador DICOM nesta etapa; o download do ZIP é suficiente.

## Segurança e privacidade

- Não colocar dados reais de pacientes em dados de demonstração, screenshots públicos ou logs de analytics.
- Não utilizar localStorage para token de acesso ao MiniPACS.
- Aplicar controle de acesso no servidor para cada rota, inclusive download e detalhes.
- Validar `StudyInstanceUID` como identificador recebido do MiniPACS, sem usá-lo como caminho de arquivo local.
- Preferir mensagens de erro neutras para o usuário e logs técnicos sem credenciais.
- Implementar rate limiting nas rotas internas do portal, especialmente nos downloads.

## Fora de escopo nesta fase

- Enviar DICOM À VPS pelo portal.
- Expor DICOMweb, C-FIND, C-MOVE ou a porta 8042 ao navegador.
- Compartilhar credenciais de administrador do MiniPACS.
- Permitir exclusão de estudos pelo portal.
- Viewer DICOM clínico no browser.
