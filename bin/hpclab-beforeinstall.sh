#!/bin/bash -e
DEPLOYMENT_ARCHIVE=/opt/codedeploy-agent/deployment-root/${DEPLOYMENT_GROUP_ID}/${DEPLOYMENT_ID}/deployment-archive
virtualenv /webapps/hpc-lab-maker
source /webapps/hpc-lab-maker/bin/activate
echo "Deployment archive: $DEPLOYMENT_ARCHIVE"
yum install -y gcc bintuils autoconf automake make libtool cracklib-devel \
  cracklib-python cracklib-dicts
pip install --requirement $DEPLOYMENT_ARCHIVE/requirements.txt --upgrade
