# Probes

## P0 — autenticação por assinatura

`p0_auth.py` responde uma única pergunta antes da construção da TUI completa:

> o Pinker Harness consegue usar oficialmente a assinatura já suportada pelo provider?

Execute:

```bash
python3 probes/p0_auth.py
```

O probe oferece inicialmente:

- OpenAI via Codex CLI;
- Anthropic via Claude Code.

Ele deriva o usuário do sistema da Task, permite apenas `amara` e `velina`, e
usa exclusivamente `codex-<usuário>` e `claude-<usuário>` como rotas de
credencial. A evidência registra também os wrappers resolvidos e o status de
login do Codex.

Ele prefere reutilizar a autenticação oficial já existente no CLI. Se ela não existir, invoca o fluxo oficial de login do próprio provider. Depois do login, faz uma chamada mínima real para provar que a assinatura está utilizável.

A API não é fallback do P0. Ela será validada separadamente.

## P0 — failover por quota

`p0_failover.py` prova a menor política de failover: mantém Anthropic, preserva
`Auth: PASS` quando a saída real confirma quota esgotada e tenta a mesma marker
em outra conta autorizada do provider.

```bash
python3 probes/p0_failover.py
```

Evidência de runtime usa `PINKER_HARNESS_ARTIFACT_ROOT`, ou
`/tmp/pinker-harness-artifacts`; o probe recusa qualquer raiz dentro do checkout.
Não há login, cópia de credenciais ou fallback cross-provider nesta fatia.

Hipótese de P0: `FORJA SHOULD EVENTUALLY OWN EXECUTION_IDENTITY` e
`HARNESS SHOULD OWN PROVIDER_ROUTING`; a allowlist de desenvolvimento não é a
arquitetura definitiva.

### Resultado

Por provider, o gate de assinatura é binário:

```text
PASS | DISCARDED
```

Quando disponível por superfície oficial estruturada, o probe também informa modelo e quota. Para Codex, as janelas de rate limit são mantidas separadas. Para Claude Code, quota é `UNSUPPORTED` enquanto não houver superfície headless oficial adequada; a TUI não é raspada.

Uma evidência sanitizada é gravada em:

```text
~/Downloads/pinker-harness-p0-auth-<timestamp>.json
```

O arquivo não contém token, senha, cookie ou cópia do cache de autenticação.

### Limites desta fatia

Este probe não implementa:

- TUI completa;
- API billing;
- lifecycle da Forja;
- checkpoint;
- memória;
- Guardião/hooks;
- orquestração multiagente;
- troca de conta/provider.

Essas capacidades pertencem às fases posteriores definidas na Issue #1.
