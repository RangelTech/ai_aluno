# Servidor STUN/TURN proprio (coturn) da area do aluno: a sala de mentoria nao depende de servico de terceiros.
# O segredo (TURN_SECRET) vem do arquivo de segredos da VPS; o app gera credenciais temporarias com ele.
FROM coturn/coturn:4.6.2
COPY --chmod=755 entrypoint.sh /turn-entrypoint.sh
ENTRYPOINT ["/turn-entrypoint.sh"]
