FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn apt repository whose signing key has expired.
# Any apt-get update then fails, which breaks the Docker-in-Docker feature
# install. This project needs no Yarn, so drop the repository.
RUN rm -f /etc/apt/sources.list.d/yarn.list
