# Build stage — shade the Ontop handler jar
FROM public.ecr.aws/docker/library/maven:3.9-eclipse-temurin-21 AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /src
COPY pom.xml .
RUN mvn -q -o dependency:go-offline || mvn -q dependency:go-offline
COPY src ./src
RUN mvn -q clean package -DskipTests

# Runtime stage — AWS Lambda Java 21 ARM64
FROM public.ecr.aws/lambda/java:21-arm64
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root breaks bootstrap
# checkov:skip=CKV_DOCKER_2:Lambda health managed by the runtime; HEALTHCHECK n/a
COPY --from=build /src/target/ontop-translate-1.0.0.jar ${LAMBDA_TASK_ROOT}/lib/
CMD ["com.semanticlayer.ontop.Handler::handleRequest"]
