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
package org.apache.hive.service.auth

import org.apache.spark.SparkFunSuite

class HttpAuthUtilsSuite extends SparkFunSuite {

  test("cookie token round-trips ordinary user names") {
    val token = HttpAuthUtils.createCookieToken("alice")
    assert(HttpAuthUtils.getUserNameFromCookieToken(token) === "alice")
  }

  test("user names containing cookie separator characters round-trip correctly") {
    // A user name containing '&' or '=' must round-trip rather than being
    // split into extra attributes that collide with existing keys.
    Seq("m&cu=hive", "a=b", "x&y=z&", "we ird+user", "50%off").foreach { name =>
      val token = HttpAuthUtils.createCookieToken(name)
      assert(HttpAuthUtils.getUserNameFromCookieToken(token) === name)
    }
  }

  test("tokens with duplicate attributes are rejected") {
    assert(HttpAuthUtils.getUserNameFromCookieToken("cu=alice&rn=1&cu=hive") === null)
  }

  test("malformed tokens are rejected instead of throwing") {
    assert(HttpAuthUtils.getUserNameFromCookieToken("cu=alice") === null)
    assert(HttpAuthUtils.getUserNameFromCookieToken("cu=alice&rn=1&x=y") === null)
    assert(HttpAuthUtils.getUserNameFromCookieToken("garbage") === null)
  }
}
