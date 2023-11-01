.PHONY: cog-yaml-train
cog-yaml-train:
	PREDICT_FILE=train.py envsubst < cog.yaml.in > cog.yaml

# .PHONY: cog-yaml-predict
# cog-yaml-predict:
# 	PREDICT_FILE=predict.py envsubst < cog.yaml.in > cog.yaml

.PHONY: push-train
push-train: cog-yaml-train
	cog push r8.im/chigozienri/lora-training-sdxl

# .PHONY: push-predict
# push-predict: cog-yaml-predict
# 	cog push r8.im/chigozienri/lora-sdxl

.PHONY: push
push: lint push-train
# push-predict

.PHONY: lint
lint: cog-yaml-train
	cog run isort --profile "black" .; cog run black .
