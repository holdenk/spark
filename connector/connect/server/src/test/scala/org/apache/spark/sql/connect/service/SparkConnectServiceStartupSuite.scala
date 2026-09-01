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

package org.apache.spark.sql.connect.service

import java.net.InetSocketAddress

import scala.jdk.CollectionConverters._

import org.apache.spark.SecurityManager
import org.apache.spark.internal.config.NETWORK_AUTH_ENABLED
import org.apache.spark.sql.connect.config.Connect
import org.apache.spark.sql.test.SharedSparkSession

class SparkConnectServiceStartupSuite extends SharedSparkSession {

  test("server does not start with spark.authenticate.secret and a non-loopback bind") {
    withSparkEnvConfs(
      (SecurityManager.SPARK_AUTH_SECRET_CONF, "test-secret"),
      (Connect.CONNECT_GRPC_BINDING_ADDRESS.key, "0.0.0.0")) {
      val e = intercept[IllegalArgumentException] {
        SparkConnectService.start(spark.sparkContext)
      }
      assert(e.getMessage.contains("does not support authentication"))
    }
  }

  test("server does not start with spark.authenticate and a non-loopback bind") {
    withSparkEnvConfs(
      (NETWORK_AUTH_ENABLED.key, "true"),
      (Connect.CONNECT_GRPC_BINDING_ADDRESS.key, "0.0.0.0")) {
      val e = intercept[IllegalArgumentException] {
        SparkConnectService.start(spark.sparkContext)
      }
      assert(e.getMessage.contains("does not support authentication"))
    }
  }

  test("server does not start with spark.connect.authenticate.token and a non-loopback bind") {
    withSparkEnvConfs(
      (Connect.CONNECT_AUTHENTICATE_TOKEN.key, "test-token"),
      (Connect.CONNECT_GRPC_BINDING_ADDRESS.key, "0.0.0.0")) {
      val e = intercept[IllegalArgumentException] {
        SparkConnectService.start(spark.sparkContext)
      }
      assert(e.getMessage.contains("does not support authentication"))
    }
  }

  test("server starts with a non-loopback bind when the binding check is disabled") {
    withSparkEnvConfs(
      (SecurityManager.SPARK_AUTH_SECRET_CONF, "test-secret"),
      (Connect.CONNECT_GRPC_BINDING_ADDRESS.key, "0.0.0.0"),
      (Connect.CONNECT_GRPC_BINDING_CHECK_ENABLED.key, "false"),
      (Connect.CONNECT_GRPC_BINDING_PORT.key, "0")) {
      try {
        SparkConnectService.start(spark.sparkContext)
        assert(SparkConnectService.server.getListenSockets.asScala.nonEmpty)
      } finally {
        SparkConnectService.stop()
      }
    }
  }

  test("server starts with spark.connect.authenticate.token when bound to loopback") {
    withSparkEnvConfs(
      (Connect.CONNECT_AUTHENTICATE_TOKEN.key, "test-token"),
      (Connect.CONNECT_GRPC_BINDING_PORT.key, "0")) {
      try {
        SparkConnectService.start(spark.sparkContext)
        val sockets = SparkConnectService.server.getListenSockets.asScala
        assert(sockets.nonEmpty)
        sockets.foreach { sa =>
          assert(sa.asInstanceOf[InetSocketAddress].getAddress.isLoopbackAddress)
        }
      } finally {
        SparkConnectService.stop()
      }
    }
  }

  test("server starts with spark.authenticate.secret when bound to loopback") {
    withSparkEnvConfs(
      (SecurityManager.SPARK_AUTH_SECRET_CONF, "test-secret"),
      (Connect.CONNECT_GRPC_BINDING_PORT.key, "0")) {
      try {
        SparkConnectService.start(spark.sparkContext)
        val sockets = SparkConnectService.server.getListenSockets.asScala
        assert(sockets.nonEmpty)
        sockets.foreach { sa =>
          assert(sa.asInstanceOf[InetSocketAddress].getAddress.isLoopbackAddress)
        }
      } finally {
        SparkConnectService.stop()
      }
    }
  }
}
