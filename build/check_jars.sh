#!/usr/bin/env bash

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Print out the native artifacts & list jars (discover new deps)
tempfileso=$(mktemp sparkBuildSOXXXXXXXX)
tempfilejars=$(mktemp sparkBuildJARXXXXXXXX)
for file in $(find . -name "*.jar" -path "*target/jars/*" -type f); do
  echo "${file}" >> "${tempfilejars}"
  unzip -Z1 "${file}" | grep '.*\.so$' >> "${tempfileso}" || echo "No so file found in ${file}"
done
if [[ -r "${tempfileso}" ]]; then
  echo "***FOUND NATIVE CODE LIBRARIES***"
  cat "${tempfileso}" | sort | uniq > "${tempfileso}uniqso"
  echo "Computing diff from expected."
  diff -w uniqso uniqexpectedso || (
    echo "***NEW SO FILE INTRODUCED IF THIS IS INTENTIONAL UPDATE uniqueexpecedso***";
    exit 1
  )
fi
cat "${tempfilejars}" |sort | uniq > "${tempfilejars}uniq"
echo "Packaged jars:"
cat "${tempfilejars}uniq"
echo "Did we introduce new JARs?"
diff -w uniqjars uniqexpectedjars || (
  echo "***DANGER NEW JAR INTRODUCED: If this intentional update uniqexpectedjars***";
  exit 1
)
set -x
