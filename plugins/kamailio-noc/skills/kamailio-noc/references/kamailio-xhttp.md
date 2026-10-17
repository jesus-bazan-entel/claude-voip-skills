# Endpoints xhttp opcionales (/healthz, /readyz, /metrics)

Sin ellos el NOC funciona (liveness por `kamcmd`, readiness por consulta MySQL directa).
Con ellos, un balanceador/K8s puede chequear Kamailio y el NOC usa Prometheus como
respaldo de `kamcmd stats`. **Requiere reiniciar Kamailio → ventana de mantenimiento y
confirmación del usuario.**

```cfg
listen=tcp:127.0.0.1:8080
loadmodule "xhttp.so"
loadmodule "xhttp_prom.so"
modparam("xhttp_prom", "xhttp_prom_stats", "all")
modparam("xhttp_prom", "xhttp_prom_beginning", "kamailio_")

event_route[xhttp:request] {
    if ($si != "127.0.0.1") { xhttp_reply("403", "Forbidden", "text/plain", "deny\n"); exit; }
    if ($hu =~ "^/healthz") {
        xhttp_reply("200", "OK", "application/json", "{\"status\":\"up\",\"ts\":$Ts}\n"); exit;
    }
    if ($hu =~ "^/readyz") {
        # requiere sqlops con una conexión "ca" definida
        if (!sql_query("ca", "SELECT 1 AS ok", "ra")) {
            xhttp_reply("503", "Unavailable", "application/json", "{\"db\":\"unreachable\"}\n"); exit;
        }
        sql_result_free("ra");
        xhttp_reply("200", "OK", "application/json", "{\"status\":\"ready\"}\n"); exit;
    }
    if (prom_check_uri()) { prom_dispatch(); exit; }
    xhttp_reply("404", "Not Found", "text/plain", "use /healthz /readyz /metrics\n"); exit;
}
```

Luego `"kamailio_http": "http://127.0.0.1:8080"` en `noc.conf.json`.
Ojo: si `tcp` no estaba habilitado, `listen=tcp:` activa workers TCP (`tcp_children`).
