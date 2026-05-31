COMPOSE = docker compose -f docker-compose.yml --env-file .env

.PHONY: up down restart logs status pull init \
        frigate-up frigate-logs frigate-config \
        bridge-logs auth-ui frigate-ui lint check-env

# ── Lifecycle ─────────────────────────────────────────────────────────────────

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

status:
	$(COMPOSE) ps

pull:
	$(COMPOSE) pull

# ── First-run / restore ───────────────────────────────────────────────────────

# Start all services and push configs into named volumes via docker cp.
# Run this after a fresh deploy or after `make down` on a new host.
init:
	@echo "Starting mosquitto..."
	$(COMPOSE) up -d mosquitto
	@sleep 3
	@echo "Starting ring-mqtt..."
	$(COMPOSE) up -d ring-mqtt
	@echo "Waiting for ring-mqtt to initialize..."
	@sleep 5
	@echo "Pushing ring-mqtt config and auth state..."
	docker cp ring-mqtt-data/config.json ring-rtsp-bridge:/data/config.json
	docker cp ring-mqtt-data/ring-state.json ring-rtsp-bridge:/data/ring-state.json
	@echo "Starting Frigate..."
	$(COMPOSE) up -d frigate
	@echo "Waiting for Frigate to initialize..."
	@sleep 20
	@echo "Pushing Frigate config..."
	docker cp frigate-config/config.yaml ring-frigate:/config/config.yaml
	docker restart ring-frigate
	@echo ""
	@echo "Done. Stack is up."
	@echo "Frigate UI: http://$$(hostname -I | awk '{print $$1}'):5000/"
	@echo "Ring auth:  http://$$(hostname -I | awk '{print $$1}'):55123/"

# ── Frigate config update ─────────────────────────────────────────────────────

# Push updated frigate-config/config.yaml and restart Frigate.
frigate-config:
	docker cp frigate-config/config.yaml ring-frigate:/config/config.yaml
	docker restart ring-frigate
	@echo "Frigate restarted with new config."

# ── Logs ──────────────────────────────────────────────────────────────────────

logs:
	$(COMPOSE) logs -f --tail=100

frigate-logs:
	$(COMPOSE) logs -f --tail=100 frigate

bridge-logs:
	$(COMPOSE) logs -f --tail=100 ring-mqtt

# ── Info ──────────────────────────────────────────────────────────────────────

auth-ui:
	@echo "Ring auth UI: http://$(shell hostname -I | awk '{print $$1}'):55123/"

frigate-ui:
	@echo "Frigate UI:   http://$(shell hostname -I | awk '{print $$1}'):5000/"

# ── Validation ────────────────────────────────────────────────────────────────

lint:
	@echo "--- docker-compose.yml ---"
	$(COMPOSE) config --quiet && echo "OK: docker-compose.yml valid"
	@echo "--- required files ---"
	@test -f frigate-config/config.yaml && echo "OK: frigate-config/config.yaml" || echo "MISSING: frigate-config/config.yaml"
	@test -f ring-mqtt-data/config.json && echo "OK: ring-mqtt-data/config.json" || echo "MISSING: ring-mqtt-data/config.json"
	@test -f ring-mqtt-data/ring-state.json && echo "OK: ring-mqtt-data/ring-state.json" || echo "MISSING: ring-mqtt-data/ring-state.json (run Ring auth first)"
	@test -f .env && echo "OK: .env" || echo "MISSING: .env (copy from .env.example)"

check-env:
	@echo "--- .env values ---"
	@grep -v '^#' .env | grep -v '^$$' | while read line; do \
		key=$$(echo $$line | cut -d= -f1); \
		val=$$(echo $$line | cut -d= -f2-); \
		if echo "$$val" | grep -qE '^(change_me|your_camera_id_here)$$'; then \
			echo "WARN: $$key = $$val  ← нужно изменить"; \
		else \
			echo "OK:   $$key = $$val"; \
		fi; \
	done
