# E-mails da área do aluno

Tudo que o sistema envia por e-mail está nesta pasta. Edite os arquivos e faça um novo release; nada no código precisa mudar.

| Arquivo | Quando é enviado |
|---|---|
| `acesso.html` / `acesso.txt` | Compra aprovada: login, senha temporária (troca obrigatória no primeiro acesso) e botão para entrar |
| `redefinir.html` / `redefinir.txt` | "Esqueci a senha": link de uso único para criar uma nova |
| `oferta.html` / `oferta.txt` | Checkout abandonado: oferta mais leve (downsell) |
| `_layout.html` | Moldura de todos (topo, cores, rodapé). Mude aqui para mudar todos de uma vez |
| `assuntos.json` | Assunto de cada e-mail |

- `.html` é a versão bonita; `.txt` é a alternativa em texto puro (clientes de e-mail antigos e a mensagem do WhatsApp).
- As variáveis aparecem entre chaves duplas, como `{{ nome }}`. Cada arquivo lista no topo as que ele recebe.
- **Não coloque preços**: eles mudam no painel de produtos e o e-mail ficaria desatualizado.
- Cores da marca: fundo `#04070d`, cartão `#0b1426`, borda `#22365c`, azul `#5eb0ff` / `#9cc9ff`, texto `#e8eef8`, texto suave `#8fa3c2`.
- E-mail aceita pouco CSS: mantenha tabelas e estilos inline, como já está.
- Para ver como ficou sem comprar nada: painel de administração → Produtos e vendas → **E-mails** (pré-visualização e envio de exemplo para a caixa de teste).
