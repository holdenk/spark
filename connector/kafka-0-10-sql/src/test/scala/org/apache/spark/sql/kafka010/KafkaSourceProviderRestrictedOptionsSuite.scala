/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.spark.sql.kafka010

import org.apache.spark.SparkFunSuite

/**
 * Tests for the Kafka parameters held back from data source options by
 * KafkaSourceProvider.validateRestrictedKafkaParams. Keys here are the Kafka client config
 * names, i.e. the data source option names with the "kafka." prefix already stripped.
 */
class KafkaSourceProviderRestrictedOptionsSuite extends SparkFunSuite {

  private val base = Map("bootstrap.servers" -> "host:9092")

  test("ordinary kafka parameters pass validation") {
    KafkaSourceProvider.validateRestrictedKafkaParams(base)
    KafkaSourceProvider.validateRestrictedKafkaParams(
      base + ("security.protocol" -> "SASL_SSL") + ("sasl.mechanism" -> "SCRAM-SHA-512"))
  }

  test("class-loading kafka parameters are refused") {
    KafkaSourceProvider.CLASS_LOADING_KAFKA_CONFIGS.foreach { key =>
      val ex = intercept[IllegalArgumentException] {
        KafkaSourceProvider.validateRestrictedKafkaParams(base + (key -> "com.example.Clazz"))
      }
      assert(ex.getMessage.contains("not supported"))
      assert(ex.getMessage.contains(key))
    }
    // Case variations of the key are refused too
    intercept[IllegalArgumentException] {
      KafkaSourceProvider.validateRestrictedKafkaParams(
        base + ("Metric.Reporters" -> "com.example.C"))
    }
  }

  test("sasl.jaas.config with an unsupported login module is refused") {
    val jaasValue = "com.sun.security.auth.module.JndiLoginModule required " +
      "user.provider.url=\"ldap://example:1389/a\";"
    val ex = intercept[IllegalArgumentException] {
      KafkaSourceProvider.validateRestrictedKafkaParams(base + ("sasl.jaas.config" -> jaasValue))
    }
    assert(ex.getMessage.contains("JndiLoginModule"))

    intercept[IllegalArgumentException] {
      KafkaSourceProvider.validateRestrictedKafkaParams(base +
        ("sasl.jaas.config" ->
          "com.sun.security.auth.module.LdapLoginModule REQUIRED;"))
    }
    // Case variations of the module name are refused too
    intercept[IllegalArgumentException] {
      KafkaSourceProvider.validateRestrictedKafkaParams(base +
        ("SASL.JAAS.CONFIG" ->
          "COM.SUN.SECURITY.AUTH.MODULE.JNDILOGINMODULE required;"))
    }
  }

  test("legitimate sasl.jaas.config login modules stay usable") {
    KafkaSourceProvider.validateRestrictedKafkaParams(base +
      ("sasl.jaas.config" ->
        ("org.apache.kafka.common.security.scram.ScramLoginModule required " +
          "username=\"u\" password=\"p\";")))
    KafkaSourceProvider.validateRestrictedKafkaParams(base +
      ("sasl.jaas.config" ->
        "org.apache.kafka.common.security.plain.PlainLoginModule required;"))
  }

  test("admin opt-out system property lets restricted options through") {
    System.setProperty(KafkaSourceProvider.ALLOW_RESTRICTED_OPTIONS_PROP, "true")
    try {
      KafkaSourceProvider.validateRestrictedKafkaParams(
        base + ("interceptor.classes" -> "com.example.Interceptor") +
          ("sasl.jaas.config" ->
            "com.sun.security.auth.module.JndiLoginModule required;"))
    } finally {
      System.clearProperty(KafkaSourceProvider.ALLOW_RESTRICTED_OPTIONS_PROP)
    }
  }
}
